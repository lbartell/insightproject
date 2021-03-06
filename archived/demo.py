#!/usr/bin/python
import argparse
# import psycopg2
from config import config
from sqlalchemy import create_engine
# from sqlalchemy_utils import database_exists, create_database
# import psycopg2
import pandas as pd
from matplotlib import pyplot as plt
import numpy as np
# from sklearn import datasets, linear_model
from sklearn.model_selection import train_test_split
import seaborn as sns
import statsmodels.formula.api as smf
from statsmodels.sandbox.regression.predstd import wls_prediction_std
import re
from sklearn import linear_model

# Input parser
parser = argparse.ArgumentParser(
    description='Analyze clinical trial dropout rates.')
parser.add_argument('--getdata', dest='getdata', action='store_const',
                    const=True, default=False,
                    help='Gather data of interest (default: do not get data)')
parser.add_argument('--savedata', dest='savedata', action='store_const', 
                    const=True, default=False,
                    help='Save dataframe (default: do not save data)')
parser.add_argument('--loaddata', dest='loaddata', action='store', default=None,
                    help='Load dataframe from given filename (default: None, do not load data)')
parser.add_argument('--plot', dest='plot', action='store_const',
                    const=True, default=False,
                    help='Create various plots (default: do not plot stuff)')
parser.add_argument('--fit', dest='fit', action='store_const',
                    const=True, default=False,
                    help='Fit linear model (default: do not fit model)')

# Custom settings
# pd.set_option('display.width', 150)
sns.set(style="white", color_codes='default', context='talk')

# ===================== Functions to gather data
def _connectdb_():
    """ Open and return SQLAlchemy engine to PostgreSQL database """

    # read connection parameters
    params = config()

    # connect to the PostgreSQL server
    engine = create_engine('postgresql://%s:%s@%s/%s' %
                           (params['user'], params['password'],
                            params['host'], params['database']))

    return engine


def gather_response():
    """ Connect to AACT postgres database and collect response variables (number
    of participants enrolled and dropped), with some consistency checks

    Returns:
        df (DataFrame): Pandas dataframe with columns for study ID ('nct_id'), 
                        number of participants enrolled ('enrolled') at the 
                        start, and the number that dropped out ('dropped')

    Notes:
    - Only keep studies with valid (non-nan) data
    - Only keep studies where all of the following are true:
        a. the total number of participants dropped equals the number 'NOT
           COMPLETED' 
        b. the number of participants 'STARTED' equals the number 'COMPLETED'
           plus the number 'NOT COMPLETED'
        c. the number of participants 'STARTED' equals the number 'enrolled'
    """

    # Connect to AACT database
    engine = _connectdb_()


    # Gather enrollment/dropout numbers - PART 1a
    #   Gather dropout info from the 'drop_withdrawals' table by summing
    #   the total count of people that dropped out within each study
    colnames = {'nct_id': 'nct_id', 
                'count':'dropped'}
    df = pd.read_sql_table('drop_withdrawals', engine,
                           columns=colnames.keys()
                           ).groupby('nct_id').sum().rename(columns=colnames)

    # Gather enrollment/dropout numbers - PART 1b
    #   Gather enrollment numbers (actual, not anticipated) from 'studies' table
    #   and append to existing dataframe
    colnames = {'nct_id':'nct_id', 
                'enrollment':'enrolled', 
                'enrollment_type': 'enrollment_type'}
    studies = pd.read_sql_table('studies', engine, 
                                columns=colnames.keys()
                                ).set_index('nct_id').rename(columns=colnames)
    filt = [x=='Actual' for x in studies['enrollment_type']]
    df = df.join(studies[filt][['enrolled']].astype(int), how='inner')
    df.dropna(how='any', inplace=True)

    # Gather enrollment/dropout numbers - PART 2
    #   Gather enrollment and dropout numbers from the 'milestones' table, only
    #   looking at the COMPLTED/STARTED/NOT COMPLETED counts, and append to 
    #   existing dataframe
    colnames = {'nct_id': 'nct_id', 
                'title': 'milestone_title', 
                'count':'milestone_count'}
    df2 = pd.read_sql_table('milestones', engine, columns=colnames.keys())
    value_str = ['COMPLETED', 'STARTED', 'NOT COMPLETED']
    for s in value_str:
        filt = df2['title'].str.match(s)
        df = df.join(df2[filt][['nct_id','count']] \
            .groupby('nct_id').sum().rename(columns={'count':s}), how='inner')

    # Check the various enrollment measures against each other and only keep 
    # studies that make sense
    filt = ((df['enrolled'] == df['STARTED']) & 
            (df['dropped'] == df['NOT COMPLETED']) &
            (df['STARTED'] == (df['NOT COMPLETED']+df['COMPLETED'])))
    df = df[filt]

    # return limited dataframe
    df = df[['enrolled', 'dropped']]
    return df


def gather_features():
    """ Connect to AACT database, join select data, and return as a dataframe

    Return:
        df (DataFrame): pandas dataframe with full data

    Notes:
    - filter for Completed & Inverventional studies only
    - Creates dummy variables
    - Only keep top N most common terms for 'browse_conditions', 
      'browse_interventions', and 'keywords' (N is hardcoded in this function)
    """

    """ Notes to self
    table_names = [
        # 'baseline_counts',              # x
        'baseline_measurements',        # Y male/female [category, param_value_num]
        # 'brief_summaries',              # ~ long text description
        'browse_conditions',            # Y mesh terms of disease (3700) -> heirarchy, ID --> Get this!
        'browse_interventions',         # Y mesh terms of treatment (~3000)
        'calculated_values',            # Y [number_of_facilities, registered_in_calendar_year, registered_in_calendar_year, registered_in_calendar_year, min age, max age]
        # 'conditions',                   # x condition name
        # 'countries',                    # ~ Country name
        # 'design_group_interventions',   # x
        # 'design_groups'                 # x
        # 'design_outcomes',              # x
        # 'designs',                      # x~ subject/caregiver/investigator blinded?
        # 'detailed_descriptions',        # x 
        # 'drop_withdrawals',             # Y --> already in response
        # 'eligibilities',                # Y (genders) --> Already got from baseline?
        # 'facilities',                   # x
        # 'intervention_other_names',     # x
        'interventions',                # Y intervetion_type (11)
        'keywords',                     # Y downcase_name (160,000!)
        # 'milestones',                   # Y title (NOT COMPLETE/COMPLETED, 90,000) and count --> already in response
        # 'outcomes',                     # x
        # 'participant_flows',            # x
        # 'reported_events',              # x
        # 'result_groups',                # x
        'studies'                       # Y [study_type, overall_status (filt), phase (parse), number_of_arms, number_of_groups, has_dmc, is_fda_regulated_drug, is_fda_regulated_device, is_unapproved_device]
    ]
    """

    # Connect to database
    engine = _connectdb_()

    # ================ Gather fe/male counts from 'baseline_measurements'
    colnames = {'nct_id': 'nct_id',
                'category': 'category',
                'classification': 'classification',
                'param_value_num': 'count'}
    meas = pd.read_sql_table('baseline_measurements', engine,
                             columns=colnames.keys()).rename(columns=colnames)
    meas.set_index('nct_id', inplace=True)

    # Determine if these particpant group counts are for fe/male
    sexes = ['male', 'female']
    for s in sexes:
        filt = (meas['category'].str.lower().str.match(s) |
                meas['classification'].str.lower().str.match(s))
        meas[s] = np.NaN
        meas.loc[filt, s] = meas[filt]['count']

    # Group/sum by study id, forcing those with no info back to nans
    noinfo = meas[sexes].groupby('nct_id').apply(lambda x: True if np.all(np.isnan(x)) else False)
    meas = meas[sexes].groupby('nct_id').sum()
    meas.loc[noinfo, sexes] = np.NaN
    # ================ 

    # ================ Gather condition MeSH terms from 'browse_conditions' (only keep N most common)
    N = 5
    colnames = {'nct_id': 'nct_id',
                'mesh_term': 'cond'}
    conds = pd.read_sql_table('browse_conditions', engine,
                              columns=colnames.keys()
                              ).rename(columns=colnames).set_index('nct_id')
    conds['cond'] = conds['cond'].str.lower()
    topN_conds = conds['cond'].value_counts().head(N).index.tolist()
    conds['cond'] = [re.sub(r'[^a-z]', '', x) if x in topN_conds
                     else None for x in conds['cond']]
    conds = pd.get_dummies(conds).groupby('nct_id').any()
    # ================ 

    # ================ Gather intervention MeSH terms from 'browse_interventions' (only keep N most common)
    N = 5
    colnames = {'nct_id': 'nct_id',
                'mesh_term': 'intv'}    
    intv = pd.read_sql_table('browse_interventions', engine,
                             columns=colnames.keys()
                             ).rename(columns=colnames).set_index('nct_id')
    intv['intv'] = intv['intv'].str.lower()
    topN_intv = intv['intv'].value_counts().head(N).index.tolist()
    intv['intv'] = [re.sub(r'[^a-z]', '', x) if x in topN_intv 
                    else None for x in intv['intv']]
    intv = pd.get_dummies(intv).groupby('nct_id').any()
    # ================ 


    # ================ Gather various info from 'calculated_values'  
    colnames = {'nct_id': 'nct_id',
                'number_of_facilities': 'facilities',
                'registered_in_calendar_year': 'year',
                'actual_duration': 'duration',
                'has_us_facility': 'usfacility',
                'minimum_age_num': 'minimum_age_num',
                'maximum_age_num': 'maximum_age_num',
                'minimum_age_unit': 'minimum_age_unit',
                'maximum_age_unit': 'maximum_age_unit'}
    calc = pd.read_sql_table('calculated_values', engine,
                             columns=colnames.keys()
                             ).rename(columns=colnames).set_index('nct_id')

    # convert age units into years
    unit_map = {'year': 1., 'month':1/12., 'week': 1/52.1429,
                'day': 1/365.2422, 'hour': 1/8760., 'minute': 1/525600.}
    for s in ['minimum_age', 'maximum_age']:
        calc[s+'_unit'] = [re.sub(r's$', '', x).strip() if x is not None else None
                   for x in calc[s+'_unit'].str.lower()]
        calc[s+'_factor'] = calc[s+'_unit'].map(unit_map)
        calc[s+'_years'] = calc[s+'_num'] * calc[s+'_factor']

    # only keep colums we need, & rename some
    colnames = {'facilities': 'facilities',
                'year': 'year',
                'duration': 'duration',
                'usfacility': 'usfacility',
                'minimum_age_years': 'minage',
                'maximum_age_years': 'maxage'}
    calc = calc[list(colnames.keys())].rename(columns=colnames)
    # ================ 

    # ================ Gather intervention type info from 'interventions' 
    colnames = {'nct_id': 'nct_id',
                'intervention_type': 'intvtype'}
    intvtype = pd.read_sql_table('interventions', engine,
                             columns=colnames.keys()
                             ).rename(columns=colnames).set_index('nct_id')
    
    # drop duplicates
    intvtype = intvtype[~intvtype.index.duplicated(keep='first')]

    # convert to lowercase, remove non-alphabetic characters
    intvtype['intvtype'] = [re.sub(r'[^a-z]', '', x) 
                        for x in intvtype['intvtype'].str.lower()]
    intvtype = pd.get_dummies(intvtype).groupby('nct_id').any()
    # ================ 

    # ================ Gather keywords info from 'keywords' (only keep top N)
    N = 5
    colnames = {'nct_id': 'nct_id',
                'name': 'keyword'}
    words = pd.read_sql_table('keywords', engine,
                              columns=colnames.keys()
                              ).rename(columns=colnames).set_index('nct_id')
    words['keyword'] = words['keyword'].str.lower()
    topN_words = words['keyword'].value_counts().head(N).index.tolist()
    words['keyword'] = [re.sub(r'[^a-z]', '', x) if x in topN_words
                    else None for x in words['keyword']]
    words = pd.get_dummies(words).groupby('nct_id').any()
    # ================ 

    # ================ Gather various info from 'studies' (filter for Completed & Inverventional studies only!)
    colnames = {'nct_id': 'nct_id',
                'study_type': 'studytype',
                'overall_status': 'status',
                'phase': 'phase',
                'number_of_arms': 'arms'}
    studies = pd.read_sql_table('studies', engine,
                                columns=colnames.keys()
                                ).rename(columns=colnames).set_index('nct_id')
    
    # filter to only keep 'Completed' studies
    filt = (studies['status'].str.match('Completed') & 
            studies['studytype'].str.match('Interventional'))
    studies = studies[filt].drop(columns=['status', 'studytype'])

    # parse study phases
    for n in [1,2,3, 4]:
        filt = studies['phase'].str.contains(str(n))
        studies['phase'+str(n)] = False
        studies.loc[filt,'phase'+str(n)] = True
    studies.drop(columns=['phase'], inplace=True)
    # ================ 

    # ================ Combine all dataframes together!
    # Note: left join all data onto 'studies' (so only keep data for completed, 
    # interventional studies)

    df = studies
    for d in [meas, conds, intv, calc, intvtype, words]:
        df = df.join(d, how='left')

    return df


def remove_highdrops(df, thresh=1.0):
    """ Given dataframe, remove rows where the dropout rate is above thresh, and
    return the resulting dataframe
    """
    return df[df['dropped'] < df['enrolled']*thresh]


def get_data(savename=None):
    """ Connect to AACT database and gather data of interest
    
    Kwargs:
        savename (string): If not None, save the resulting DataFrame with
                           data to this file name using pickle
    
    Return:
        df (DataFrame): Pandas DataFrame with data features and responses
    """

    # Collect data (features & response, inner join)
    df = gather_features().join(gather_response(), how='inner')

    # Remove 100% dropouts
    df = remove_highdrops(df)

    # Fill some NaNs with default values, where appropriate
    colstofill = []
    for c in list(df.columns):
        if '_' in c:
            colstofill.append(c)
    for c in colstofill:
        df[c].fillna(False, inplace=True)

    # Save
    if savename is not None:
        df.to_pickle(savename)

    # Return
    return df


def make_pair_corr_plots(df):
    """ Create & show grid plot and correlation plot for non dummies
    """

    cols = []
    for c in list(df.columns):
        if (c.find('_')<0 and c.find('phase')<0 and c.find('groups')<0
            and c.find('dropped')<0 and c.find('enrolled')<0):
            cols.append(c)

    sns.set(font_scale=0.75) 

    # GRID PLOT
    g = sns.PairGrid(df[cols].dropna(), size=1)
    g = g.map_diag(plt.hist)
    g = g.map_offdiag(plt.scatter, s=5, alpha=0.5)
    plt.tight_layout()
    plt.show()

    # Corr coefs plot
    # Corr coefs plot
    cm =df[cols].corr().as_matrix()
    hm = sns.heatmap(cm,
        cbar=True,
        annot=True,
        square=True,
        fmt='.2f',
        annot_kws={'size': 7},
        yticklabels=cols,
        xticklabels=cols)
    plt.yticks(rotation=0) 
    plt.xticks(rotation=90) 
    plt.show()


def all_feature_plots(df, response_name='dropped', show=False, savedir=None):
    """ Given data table, plot the response against each feature

    Args:
        df (DataFrame): pandas dataframe with data to plot
        response_name (str): string specifying the column to use as response 
                             variable
        show (bool): If true, use matplotlib.pyplot.show to render each plot, 
                     otherwise close on completion
        savedir (str): String specifying directory/folder to save plots. If 
                       none, do not save. Plots are saved in the given folder as 
                       "[feature_name] vs [response_name].png"
    Returns:
        f (list): list of figure (handle, axes) tuples for the plots created
    """

    feature_names = df.columns[[response_name not in c for c in df.columns]]
    sns.set_style("ticks")
    f = []

    # Plot response vs features
    for name in feature_names:
        #  setup plot
        fig, ax = plt.subplots(figsize=(5,4))

        # Plot data (infer categorical or continuous for box vs scatter plot)
        if len(df[name].unique())<5:
            # Box plot
            sns.boxplot(x=name, y=response_name, data=df)

        else:
            # Scatter plot
            fig, ax = plt.subplots(figsize=(5,4))
            sns.regplot(x=name, y=response_name, data=df,
                        scatter_kws={'alpha':0.3, 's':3})

        # Label plot
        ax.set(xlabel=name, ylabel=response_name, ylim=(0.,1.))
        fig.tight_layout()
        f.append((fig, ax))

        # save to file
        if savedir is not None:
            fig.savefig('{}/{} vs {}.png'.format(savedir, response_name, name))

        if show:
            fig.show()
        else:
            plt.close(fig)

    return f


# ===================== Functions regarding model/fitting
def split_data(df, save_suffix=None, test_size=None):
    """ Given data frame, split into training and text sets and save via pickle

    Args:
        df (DataFrame): pandas dataframe with data to split

    Kwargs:
        save_suffix (str): If not none save the training and testing data via 
            pickle, and append this to the filename (e.g. 
            'training_<save_suffix>.pkl' or 'testing_<save_suffix>.pkl'
        test_size (float, int, or None): proportion of data to include in test 
            set, see train_test_split() documentation for more
    
    Returns:
        dfsplit (list): 2-element list with training and testing dataframes, 
            respectively (as output by train_test_split)
    """
    dfsplit = train_test_split(df)

    # Save training and testing data
    if save_suffix is not None:
        dfsplit[0].to_pickle('training_{}.pkl'.format(save_suffix))
        dfsplit[1].to_pickle('testing_{}.pkl'.format(save_suffix))
    
    return dfsplit


def fit_model(df, savename=None):
    """ Given input dataframe, run statsmodel linearfit and return results

    Args:
        df (DataFrame): pandas dataframe with data to fit
        savename (str): pickle filename for where to save the results object. If
                        None, do not save

    Returns:
        res (statsmodels results): results of the fit
    """

    # Build formula for linear model (one linear term for each data column)
    feature_names = df.columns[['droprate' not in c for c in df.columns]]
    iscat = False
    formula = ['droprate ~ ']
    for name in feature_names:
        formula.append('+')
        
        # Determine if categorical variable and add to formula accordingly
        iscat = len(df[name].unique()) < 5
        if iscat:
            formula.append('C(')
        formula.append(name)
        if iscat:
            formula.append(')')
    formulastr = ''.join(formula)

    # Setup model
    model = smf.ols(formulastr, data=df)

    # Fit model
    res = model.fit()

    # Save to pickle
    if savename is not None:
        res.save(savename)

    return res


def diagnotic_plots(res, show=False):
    """ Create diagnostic plots from regression results object (residuals)
    
    Args:
        res (statsmodels.regression.linear_model.RegressionResultsWrapper):
            Results of fitting linear regression model

    Kwargs:
        show (bool): If true, call the matplotlib.pyplot.show on each figure
                     before exiting (default: False)

    Return:
        fig (tuple of matplotlib.figure.Figure): figure handles to...
            fig[0]  Historam  of fit residuals (check normality)
            fig[1]  Plot of predicted values vs residuals (check homogeneity)
    """

    # Histogram of residuals
    f1, ax1 = plt.figure(figsize=(6,3)), plt.axes()
    sns.distplot(res.resid, bins=50, kde=False)
    sns.despine(left=True)
    ax1.set(yticks=[], xlabel='residual')
    ax1.set()
    f1.tight_layout()

    # Plot residual vs predicted (homogeneous)
    sns.set_style("white") 
    f2, ax2 = plt.figure(figsize=(6,3)), plt.axes()

    plt.plot(res.predict(), res.resid.values, '.', ms=5, alpha=0.5)
    ax2.set(xlabel='predicted', ylabel='residual')
    f2.tight_layout()
    sns.despine()

    if show:
        f1.show()
        f2.show()

    return (f1, f2)


def eval_preds(df, res): # IN PROGRESS
    """ Given test data and statsmodel linear model fit, caculate RMS error
    Args:
        df (DataFrame): pandas.DataFrame with data to predict
        res (statsmodels results structure): Contains fitted model results
    
    Returns:
        Rsq (float): Coefficient of determination
    """

    # Actual vs predicted
    actual = df['droprate'].to_frame(name='actual')
    pred = res.predict(df).to_frame(name='predicted')
    errordf = actual.join(pred)
    errordf.dropna(inplace=True)
    errordf['resid'] = (errordf['actual']-errordf['predicted'])

    # Calculate Rsquared
    y = errordf['actual']
    yavg = y.mean()
    yfit = errordf['predicted']
    SStot = ((y-yavg)**2).sum()
    SSres = ((y-yfit)**2).sum()  
    Rsq = 1 - (SSres/SStot)

    return Rsq


# ===================== MAIN
if __name__ == "__main__":
    # Gather command line options
    args = parser.parse_args()

    # Get data
    df = None
    if args.loaddata is not None:
        # Load existing data (first choice)
        df = pd.read_pickle(args.loaddata)       

    elif args.getdata:
        # Save this new data?
        savename = None
        if args.savedata:
            savename='data.pkl'

        #  Gather new data
        df = get_data(savename=None)

        # Split out test/training data
        dfsplit = train_test_split(df)

        # Save training and testing data
        dfsplit[0].to_pickle('training_data.pkl')
        dfsplit[1].to_pickle('testing_data.pkl')

    # Plot stuff
    if args.plot and df is not None:

        # Number of study participants histogram
        f, ax = plt.subplots(figsize=(5, 4))
        sns.distplot(df['enrolled'],
                     bins=np.linspace(0, 1000, num=100),
                     kde=False)
        sns.despine(left=True)
        ax.set(yticks=[], xlabel='Participants enrolled')
        f.tight_layout()
        f.show()

        # Dropout rate +/- modification
        f, ax = plt.subplots(figsize=(5, 4))
        kwargs = {'bins': np.linspace(0, 1.1, 110), 'kde': False,
                  'norm_hist': True}
        sns.distplot(df['droprate'], **kwargs, label='raw')
        sns.distplot(df['droprate_tform'], **kwargs, label='transformed')
        sns.despine(left=True)
        ax.set(yticks=[], xlabel='dropout rate (fraction)')
        ax.legend()
        f.tight_layout()
        f.show()

        # Dropout rate +/- cancer
        f, ax = plt.subplots(figsize=(5, 4))
        kwargs = {'bins': np.linspace(0, 1.1, 50),
                  'kde': False, 'norm_hist': True}
        sns.distplot(df[df['is_cancer'].notnull() & df['is_cancer'] == True]['droprate_tform'],
                     **kwargs, label='cancer')
        sns.distplot(df[df['is_cancer'].notnull() & df['is_cancer'] == False]['droprate_tform'],
                     **kwargs, label='not cancer')
        sns.despine(left=True)
        ax.set(yticks=[], xlabel='dropout rate (fraction, transformed)')
        ax.legend()
        f.tight_layout()
        f.show()

        # Study duration histogram
        f, ax = plt.subplots(figsize=(5, 4))
        sns.distplot(df[df['duration'].notnull()]['duration'],
                     bins=np.linspace(0, 200, 50), kde=False)
        sns.despine(left=True)
        ax.set(yticks=[], xlabel='Study duration (months)')
        f.tight_layout()
        f.show()

    # Fit linear model
    if args.fit and df is not None:
        # Implement linear model (via statsmodels)
        formula = ('droprate**(1/2) ~ ' +
                   'duration*C(has_us_facility)*C(is_cancer)')
        model = smf.ols(formula, data=df)
        res = model.fit()
        print(res.summary())

        # Check residuals for normality, homogeneity
        for x in diagnotic_plots(res):
            x.show()

        # Get predicted values & confidence intervals
        predstd, interval_l, interval_u = wls_prediction_std(res)

        # - Gather subset of data of interest
        interval_l_df = interval_l.to_frame(name='lower')
        interval_u_df = interval_u.to_frame(name='upper')
        intervals = interval_l_df.join(interval_u_df)
        model_data = df[['duration','droprate_tform','is_cancer']].\
            join(intervals, how='inner')
        model_data['pred_droprate'] = res.predict()
        model_data = model_data.sort_values('duration')

        # - Plot predicted value / CIs
        x = model_data['duration']
        y = model_data['droprate_tform']
        ypred = model_data['pred_droprate']
        ypred_l = model_data['lower']
        ypred_u = model_data['upper']

        f, ax = plt.subplots(ncols=2, figsize=(10,4))
        for cval in [False, True]:
            filt = model_data['is_cancer']==cval
            x = model_data[filt]['duration']
            y = model_data[filt]['droprate_tform']
            yp = model_data[filt]['pred_droprate']
            yl = model_data[filt]['lower']
            yu = model_data[filt]['upper'] 
            ax[int(cval)].scatter(x, y, marker='o', alpha=0.75)
            ax[int(cval)].plot(x, yp, '-', color='k')
            ax[int(cval)].fill_between(x, yl, yu, alpha=0.25, label='95%CI')
            ax[int(cval)].set(title='is_cancer {}'.format(cval),
                              xlabel='study duration',
                              ylabel='droprate_tform')
            ax[int(cval)].legend()
        f.show()
