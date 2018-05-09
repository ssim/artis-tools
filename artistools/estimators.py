#!/usr/bin/env python3
"""Functions for reading and plotting estimator files.
Examples are temperatures, populations, heating/cooling rates.
"""
# import math
import argparse
import glob
import gzip
import math
import os
import re
import sys
from collections import namedtuple
from functools import lru_cache
from itertools import chain
from pathlib import Path

import matplotlib.pyplot as plt
# import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

import artistools as at

# from astropy import constants as const

colors_tab10 = list(plt.get_cmap('tab10')(np.linspace(0, 1.0, 10)))
elementcolors = {
    'Fe': colors_tab10[0],
    'Ni': colors_tab10[1],
    'Co': colors_tab10[2],
}

variableunits = {
    'time': 'days',
    'TR': 'K',
    'Te': 'K',
    'TJ': 'K',
    'nne': 'e-/cm3',
    'heating': 'erg/s/cm3',
    'cooling': 'erg/s/cm3',
    'velocity': 'km/s',
}

variablelongunits = {
    'TR': 'Temperature in kelvin',
    'Te': 'Temperature in kelvin',
    'TJ': 'Temperature in kelvin',
}

dictlabelreplacements = {
    'Te': 'T$_e$',
    'TR': 'T$_R$'
}


def get_elemcolor(atomic_number=None, elsymbol=None):
    assert (atomic_number is None) != (elsymbol is None)
    if atomic_number is not None:
        elsymbol = at.elsymbols[atomic_number]

    if elsymbol not in elementcolors:
        elementcolors[elsymbol] = colors_tab10[len(elementcolors)]

    return elementcolors[elsymbol]


def get_ionrecombrates_fromfile(filename):
    """WARNING: copy pasted from artis-atomic! replace with a package import soon ionstage is the lower ion stage."""
    print(f'Reading {filename}')

    header_row = []
    with open(filename, 'r') as filein:
        while True:
            line = filein.readline()
            if line.strip().startswith('TOTAL RECOMBINATION RATE'):
                line = filein.readline()
                line = filein.readline()
                header_row = filein.readline().strip().replace(' n)', '-n)').split()
                break

        if not header_row:
            print("ERROR: no header found")
            sys.exit()

        index_logt = header_row.index('log(T)')
        index_low_n = header_row.index('RRC(low-n)')
        index_tot = header_row.index('RRC(total)')

        recomb_tuple = namedtuple("recomb_tuple", ['logT', 'RRC_low_n', 'RRC_total'])
        records = []
        for line in filein:
            row = line.split()
            if row:
                if len(row) != len(header_row):
                    print('Row contains wrong number of items for header:')
                    print(header_row)
                    print(row)
                    sys.exit()
                records.append(recomb_tuple(
                    *[float(row[index]) for index in [index_logt, index_low_n, index_tot]]))

    dfrecombrates = pd.DataFrame.from_records(records, columns=recomb_tuple._fields)
    return dfrecombrates


def get_units_string(variable):
    if variable in variableunits:
        return f' [{variableunits[variable]}]'
    elif variable.split('_')[0] in variableunits:
        return f' [{variableunits[variable.split("_")[0]]}]'
    return ''


def get_ylabel(variable):
    if variable in variablelongunits:
        return variablelongunits[variable]
    elif variable in variableunits:
        return f'[{variableunits[variable]}]'
    elif variable.split('_')[0] in variableunits:
        return f'[{variableunits[variable.split("_")[0]]}]'
    return ''


def parse_ion_row(row, outdict):
    variablename = row[0]
    if row[1].endswith('='):
        atomic_number = int(row[2])
        startindex = 3
    else:
        atomic_number = int(row[1].split('=')[1])
        startindex = 2

    if variablename not in outdict:
        outdict[variablename] = {}

    for index, token in list(enumerate(row))[startindex::2]:
        try:
            ion_stage = int(token.rstrip(':'))
        except ValueError:
            print(f'Cannot parse row: {row}')
            return

        value_thision = float(row[index + 1].rstrip(','))

        outdict[variablename][(atomic_number, ion_stage)] = value_thision

        if variablename == 'populations':
            elpop = outdict[variablename].get(atomic_number, 0)
            outdict[variablename][atomic_number] = elpop + value_thision

            totalpop = outdict[variablename].get('total', 0)
            outdict[variablename]['total'] = totalpop + value_thision

        elif variablename == 'Alpha_R*nne':
            if 'Alpha_R' not in outdict:
                outdict['Alpha_R'] = {}
            outdict['Alpha_R'][(atomic_number, ion_stage)] = value_thision / outdict['nne']


@lru_cache(maxsize=16)
def read_estimators(modelpath, modelgridindex=-1, timestep=-1):
    """Read estimator files into a nested dictionary structure.

    Speed it up by only retrieving estimators for a particular timestep or modelgridindex.
    """
    match_timestep = timestep
    match_modelgridindex = modelgridindex
    modeldata, _ = at.get_modeldata(modelpath)

    if match_modelgridindex >= 0:
        mpirank = at.get_mpirankofcell(match_modelgridindex, modelpath=modelpath)
        strmpirank = f'{mpirank:04d}'
    else:
        strmpirank = '????'

    estimfiles = chain(
        Path(modelpath).rglob(f'**/estimators_{strmpirank}.out'),
        Path(modelpath).rglob(f'**/estimators_{strmpirank}.out.gz'))

    if match_modelgridindex < 0:
        npts_model = at.get_npts_model(modelpath)
        estimfiles = [x for x in estimfiles if
                      int(re.findall('[0-9]+', os.path.basename(x))[-1]) < npts_model]
        # actually number of files read may be less if some files are contained within a folder
        # that is known not to contain the required timestep
        print(f'Reading up to {len(list(estimfiles))} estimator files from {modelpath}...')

    if not estimfiles:
        print("No estimator files found")
        return False

    # set of timesteps covered by files in a directory, where the key is the absolute path of the directory
    runfolder_timesteps = {}

    # membership means that a full estimator file has been read from the folder, so all timesteps are known
    runfolder_alltimesteps_found = set()

    estimators = {}
    # sorting the paths important, because there is a duplicate estimator block (except missing heating/cooling rates)
    # when Artis restarts and here, only the first found block for each timestep, modelgridindex is kept
    for estfilepath in sorted(estimfiles):
        estfilefolderpath = estfilepath.parent.resolve()

        if (match_timestep >= 0 and
                estfilefolderpath in runfolder_alltimesteps_found and
                match_timestep not in runfolder_timesteps[estfilefolderpath]):
            # already found every the timesteps in the first file in this folder and it wasn't a match
            continue

        if match_modelgridindex >= 0:
            filesize = Path(estfilepath).stat().st_size / 1024 / 1024
            print(f'Reading {estfilepath} ({filesize:.3f} MiB)')

        opener = gzip.open if str(estfilepath).endswith('.gz') else open
        with opener(estfilepath, 'rt') as estfile:
            timestep = 0
            modelgridindex = 0
            skip_block = False
            for line in estfile:
                row = line.split()
                if not row:
                    continue

                if row[0] == 'timestep':
                    if (match_timestep >= 0 and match_modelgridindex >= 0 and
                            (match_timestep, match_modelgridindex) in estimators):
                        # found our key, so exit now!
                        return estimators

                    timestep = int(row[1])
                    modelgridindex = int(row[3])
                    # print(f'Timestep {timestep} cell {modelgridindex}')
                    if ((timestep, modelgridindex) in estimators and
                            not estimators[(timestep, modelgridindex)]['emptycell']):
                        # print(f'WARNING: duplicate estimator data for timestep {timestep} cell {modelgridindex}. '
                        #       f'Kept old (T_e {estimators[(timestep, modelgridindex)]["Te"]}), '
                        #       f'instead of new (T_e {float(row[7])})')
                        skip_block = True
                    else:
                        skip_block = False

                        if estfilefolderpath not in runfolder_timesteps:
                            runfolder_timesteps[estfilefolderpath] = set()
                        runfolder_timesteps[estfilefolderpath].add(timestep)

                        estimators[(timestep, modelgridindex)] = {}
                        estimators[(timestep, modelgridindex)]['velocity'] = modeldata['velocity'][modelgridindex]
                        emptycell = (row[4] == 'EMPTYCELL')
                        estimators[(timestep, modelgridindex)]['emptycell'] = emptycell
                        if not emptycell:
                            estimators[(timestep, modelgridindex)]['TR'] = float(row[5])
                            estimators[(timestep, modelgridindex)]['Te'] = float(row[7])
                            estimators[(timestep, modelgridindex)]['W'] = float(row[9])
                            estimators[(timestep, modelgridindex)]['TJ'] = float(row[11])
                            estimators[(timestep, modelgridindex)]['nne'] = float(row[15])

                elif row[1].startswith('Z=') and not skip_block:
                    parse_ion_row(row, estimators[(timestep, modelgridindex)])

                elif row[0] == 'heating:' and not skip_block:
                    for index, token in list(enumerate(row))[1::2]:
                        estimators[(timestep, modelgridindex)][f'heating_{token}'] = float(row[index + 1])
                    if estimators[(timestep, modelgridindex)]['heating_gamma/gamma_dep'] > 0:
                        estimators[(timestep, modelgridindex)]['gamma_dep'] = (
                            estimators[(timestep, modelgridindex)]['heating_gamma'] /
                            estimators[(timestep, modelgridindex)]['heating_gamma/gamma_dep'])

                elif row[0] == 'cooling:' and not skip_block:
                    for index, token in list(enumerate(row))[1::2]:
                        estimators[(timestep, modelgridindex)][f'cooling_{token}'] = float(row[index + 1])

        if (match_modelgridindex < 0 and
                estfilefolderpath not in runfolder_alltimesteps_found and
                match_timestep >= 0 and
                match_timestep not in runfolder_timesteps[estfilefolderpath]):
            print(f" Skipping rest of folder {estfilepath.parent.relative_to(modelpath)} because "
                  f"the first file didn't contain timestep {match_timestep}")

        runfolder_alltimesteps_found.add(estfilefolderpath)
    return estimators


def plot_init_abundances(ax, xlist, specieslist, mgilist, modelpath, **plotkwargs):
    assert len(xlist) - 1 == len(mgilist)
    modeldata, _ = at.get_modeldata(modelpath)
    abundancedata = at.get_initialabundances(modelpath)

    ax.set_ylim(ymin=0.)
    ax.set_ylim(ymax=1.0)
    for speciesstr in specieslist:
        splitvariablename = speciesstr.split('_')
        elsymbol = splitvariablename[0].strip('0123456789')
        atomic_number = at.get_atomic_number(elsymbol)
        ax.set_ylabel('Initial mass fraction')

        ylist = []
        linelabel = speciesstr
        linestyle = '-'
        for modelgridindex in mgilist:
            if speciesstr.lower() in ['ni_56', 'ni56', '56ni']:
                yvalue = modeldata.loc[modelgridindex]['X_Ni56']
                linelabel = '$^{56}$Ni'
                linestyle = '--'
            elif speciesstr.lower() in ['ni_stb', 'ni_stable']:
                yvalue = abundancedata.loc[modelgridindex][f'X_{elsymbol}'] - modeldata.loc[modelgridindex]['X_Ni56']
                linelabel = 'Stable Ni'
            elif speciesstr.lower() in ['co_56', 'co56', '56co']:
                yvalue = modeldata.loc[modelgridindex]['X_Co56']
                linelabel = '$^{56}$Co'
            elif speciesstr.lower() in ['fegrp', 'ffegroup']:
                yvalue = modeldata.loc[modelgridindex]['X_Fegroup']
            else:
                yvalue = abundancedata.loc[modelgridindex][f'X_{elsymbol}']
            ylist.append(yvalue)

        ylist.insert(0, ylist[0])
        # or ax.step(where='pre', )
        color = get_elemcolor(atomic_number=atomic_number)
        ax.plot(xlist, ylist, linewidth=1.5, label=linelabel,
                linestyle=linestyle, color=color, **plotkwargs)


def plot_multi_ion_series(
        ax, xlist, seriestype, ionlist, timesteplist, mgilist, estimators, modelpath, args, **plotkwargs):
    assert len(xlist) - 1 == len(mgilist) == len(timesteplist)
    # if seriestype == 'populations':
    #     ax.yaxis.set_major_locator(ticker.MultipleLocator(base=0.10))

    compositiondata = at.get_composition_data(modelpath)

    # decoded into numeric form, e.g., [(26, 1), (26, 2)]
    iontuplelist = [
        (at.get_atomic_number(ionstr.split(' ')[0]), at.decode_roman_numeral(ionstr.split(' ')[1]))
        for ionstr in ionlist]
    iontuplelist.sort()
    print(f'Subplot with ions: {iontuplelist}')

    prev_atomic_number = iontuplelist[0][0]
    colorindex = 0
    for atomic_number, ion_stage in iontuplelist:
        if atomic_number != prev_atomic_number:
            colorindex += 1

        if compositiondata.query('Z == @atomic_number '
                                 '& lowermost_ionstage <= @ion_stage '
                                 '& uppermost_ionstage >= @ion_stage').empty:
            print(f"WARNING: Can't plot '{seriestype}' for Z={atomic_number} ion_stage {ion_stage} "
                  f"because this ion is not in compositiondata.txt")
            continue

        if seriestype == 'populations':
            if args.ionpoptype == 'absolute':
                ax.set_ylabel('X$_{ion}$ [/cm3]')
            elif args.ionpoptype == 'elpop':
                # elcode = at.elsymbols[atomic_number]
                ax.set_ylabel('X$_{ion}$/X$_{element}$')
            elif args.ionpoptype == 'totalpop':
                ax.set_ylabel('X$_{ion}$/X$_{tot}$')
            else:
                assert False
        else:
            ax.set_ylabel(seriestype)

        ylist = []
        for modelgridindex, timestep in zip(mgilist, timesteplist):
            estim = estimators[(timestep, modelgridindex)]

            if estim['emptycell']:
                continue

            if seriestype == 'populations':
                if (atomic_number, ion_stage) not in estim['populations']:
                    print(f'Note: population for {(atomic_number, ion_stage)} not in estimators for '
                          f'cell {modelgridindex} timestep {timestep}')
                    # print(f'Keys: {estim["populations"].keys()}')
                    # raise KeyError

                nionpop = estim['populations'].get((atomic_number, ion_stage), 0.)

                try:
                    if args.ionpoptype == 'absolute':
                        yvalue = nionpop  # Plot as fraction of element population
                    elif args.ionpoptype == 'elpop':
                        elpop = estim['populations'].get(atomic_number, 0.)
                        yvalue = nionpop / elpop  # Plot as fraction of element population
                    elif args.ionpoptype == 'totalpop':
                        totalpop = estim['populations']['total']
                        yvalue = nionpop / totalpop  # Plot as fraction of total population
                    else:
                        assert False
                except ZeroDivisionError:
                    yvalue = 0.

                ylist.append(yvalue)

            # elif seriestype == 'Alpha_R':
            #     ylist.append(estim['Alpha_R*nne'].get((atomic_number, ion_stage), 0.) / estim['nne'])
            # else:
            #     ylist.append(estim[seriestype].get((atomic_number, ion_stage), 0.))
            else:
                dictvars = {}
                for k, v in estim.items():
                    if type(v) is dict:
                        dictvars[k] = v.get((atomic_number, ion_stage), 0.)
                    else:
                        dictvars[k] = v
                try:
                    yvalue = eval(seriestype, {"__builtins__": math}, dictvars)
                except ZeroDivisionError:
                    yvalue = float('NaN')
                ylist.append(yvalue)

        plotlabel = f'{at.elsymbols[atomic_number]} {at.roman_numerals[ion_stage]}'

        ylist.insert(0, ylist[0])
        dashes_list = [(5, 1), (2, 1), (6, 2), (6, 1)]
        linestyle_list = ['-.', '-', '--', (0, (4, 1, 1, 1)), ':'] + [(0, x) for x in dashes_list]
        linestyle = linestyle_list[ion_stage - 1]
        linewidth = [1.5, 1.5, 1.0, 1.0, 1.0][ion_stage - 1]
        # color = ['blue', 'green', 'red', 'cyan', 'purple', 'grey', 'brown', 'orange'][ion_stage - 1]
        # assert colorindex < 10
        # color = f'C{colorindex}'
        color = get_elemcolor(atomic_number=atomic_number)
        # or ax.step(where='pre', )
        ax.plot(xlist, ylist, linewidth=linewidth, label=plotlabel, color=color, linestyle=linestyle, **plotkwargs)
        prev_atomic_number = atomic_number

    ax.set_yscale('log')
    ymin, ymax = ax.get_ylim()
    ax.set_ylim(ymin=ymin, ymax=ymax * 10 ** (0.3 * math.log10(ymax / ymin)))


def plot_series(ax, xlist, variablename, showlegend, timesteplist, mgilist, estimators, nounits=False, **plotkwargs):
    assert len(xlist) - 1 == len(mgilist) == len(timesteplist)
    formattedvariablename = dictlabelreplacements.get(variablename, variablename)
    serieslabel = f'{formattedvariablename}'
    if not nounits:
        serieslabel += get_units_string(variablename)

    if showlegend:
        linelabel = serieslabel
    else:
        ax.set_ylabel(serieslabel)
        linelabel = None

    ylist = []
    for modelgridindex, timestep in zip(mgilist, timesteplist):
        try:
            ylist.append(eval(variablename, {"__builtins__": math}, estimators[(timestep, modelgridindex)]))
        except KeyError:
            if (timestep, modelgridindex) in estimators:
                print(f"Undefined variable: {variablename} for timestep {timestep} in cell {modelgridindex}")
            else:
                print(f'No data for cell {modelgridindex} at timestep {timestep}')
            # print(estimators[(timestep, modelgridindex)])
            sys.exit()

    ylist.insert(0, ylist[0])

    try:
        if math.log10(max(ylist) / min(ylist)) > 2:
            ax.set_yscale('log')
    except ZeroDivisionError:
        ax.set_yscale('log')

    dictcolors = {
        'Te': 'red',
        # 'heating_gamma': 'blue',
        # 'cooling_adiabatic': 'blue'
    }

    # print out the data to stdout. Maybe want to add a CSV export option at some point?
    # print(f'#cellidorvelocity {variablename}\n' + '\n'.join([f'{x}  {y}' for x, y in zip(xlist, ylist)]))

    ax.plot(xlist, ylist, linewidth=1.5, label=linelabel, color=dictcolors.get(variablename, None), **plotkwargs)


def get_xlist(xvariable, allnonemptymgilist, estimators, timesteplist, modelpath, args):
    if xvariable in ['cellid', 'modelgridindex']:
        if args.xmax >= 0:
            mgilist_out = [mgi for mgi in allnonemptymgilist if mgi <= args.xmax]
        else:
            mgilist_out = allnonemptymgilist
        xlist = mgilist_out
        timesteplist_out = timesteplist
    elif xvariable == 'timestep':
        mgilist_out = allnonemptymgilist
        xlist = timesteplist
        timesteplist_out = timesteplist
    elif xvariable == 'time':
        mgilist_out = allnonemptymgilist
        timearray = at.get_timestep_times_float(modelpath)
        xlist = [timearray[ts] for ts in timesteplist]
        timesteplist_out = timesteplist
    else:
        try:
            xlist = []
            mgilist_out = []
            timesteplist_out = []
            for modelgridindex, timestep in zip(allnonemptymgilist, timesteplist):
                xvalue = estimators[(timestep, modelgridindex)][xvariable]
                if args.xmax < 0 or xvalue <= args.xmax:
                    xlist.append(xvalue)
                    mgilist_out.append(modelgridindex)
                    timesteplist_out.append(timestep)
        except KeyError:
            if (timestep, modelgridindex) in estimators:
                print(f'Unknown x variable: {xvariable} for timestep {timestep} in cell {modelgridindex}')
            else:
                print(f'No data for cell {modelgridindex} at timestep {timestep}')
            print(estimators[(timestep, modelgridindex)])
            sys.exit()

    xlist, mgilist_out, timesteplist_out = zip(
        *[xmt for xmt in sorted(zip(xlist, mgilist_out, timesteplist_out))])
    assert len(xlist) == len(mgilist_out) == len(timesteplist_out)

    return list(xlist), list(mgilist_out), list(timesteplist_out)


def plot_subplot(ax, timesteplist, xlist, yvariables, mgilist, modelpath, estimators, args, **plotkwargs):
    assert len(xlist) - 1 == len(mgilist) == len(timesteplist)
    showlegend = False

    ylabel = 'UNDEFINED'
    sameylabel = True
    for variablename in yvariables:
        if not isinstance(variablename, str):
            pass
        elif ylabel == 'UNDEFINED':
            ylabel = get_ylabel(variablename)
        elif ylabel != get_ylabel(variablename):
            sameylabel = False
            break

    for variablename in yvariables:
        if not isinstance(variablename, str):  # it's a sequence of values
            showlegend = True
            if variablename[0] == 'initabundances':
                plot_init_abundances(ax, xlist, variablename[1], mgilist, modelpath)
            elif variablename[0] == '_ymin':
                ax.set_ylim(ymin=variablename[1])
            elif variablename[0] == '_ymax':
                ax.set_ylim(ymax=variablename[1])
            else:
                seriestype, ionlist = variablename
                plot_multi_ion_series(ax, xlist, seriestype, ionlist, timesteplist, mgilist, estimators,
                                      modelpath, args, **plotkwargs)
        else:
            showlegend = len(yvariables) > 1 or len(variablename) > 20
            plot_series(ax, xlist, variablename, showlegend, timesteplist, mgilist, estimators,
                        nounits=sameylabel, **plotkwargs)
            if showlegend and sameylabel:
                ax.set_ylabel(ylabel)

    if showlegend:
        if yvariables[0][0] == 'populations':
            ax.legend(loc='upper left', handlelength=2, ncol=3,
                      frameon=False, numpoints=1, prop={'size': 8})
        else:
            ax.legend(loc='best', handlelength=2, frameon=False, numpoints=1, prop={'size': 9})


def make_plot(modelpath, timesteplist_unfiltered, allnonemptymgilist, estimators, xvariable, plotlist,
              args, **plotkwargs):
    modelname = at.get_model_name(modelpath)
    fig, axes = plt.subplots(nrows=len(plotlist), ncols=1, sharex=True,
                             figsize=(args.figscale * at.figwidth, args.figscale * at.figwidth * 0.5 * len(plotlist)),
                             tight_layout={"pad": 0.2, "w_pad": 0.0, "h_pad": 0.0})
    if len(plotlist) == 1:
        axes = [axes]

    # ax.xaxis.set_minor_locator(ticker.MultipleLocator(base=5))

    axes[-1].set_xlabel(f'{xvariable}{get_units_string(xvariable)}')
    xlist, mgilist, timesteplist = get_xlist(
        xvariable, allnonemptymgilist, estimators, timesteplist_unfiltered, modelpath, args)
    xlist = np.insert(xlist, 0, 0.)

    xmin = args.xmin if args.xmin > 0 else min(xlist)
    xmax = args.xmax if args.xmax > 0 else max(xlist)

    for ax, yvariables in zip(axes, plotlist):
        ax.set_xlim(xmin=xmin, xmax=xmax)
        plot_subplot(ax, timesteplist, xlist, yvariables, mgilist, modelpath, estimators, args, **plotkwargs)

    if len(set(timesteplist)) == 1:  # single timestep plot
        figure_title = f'{modelname}\nTimestep {timesteplist[0]}'
        try:
            time_days = float(at.get_timestep_time(modelpath, timesteplist[0]))
        except FileNotFoundError:
            time_days = 0
        else:
            figure_title += f' ({time_days:.2f}d)'

        defaultoutputfile = Path('plotestimators_ts{timestep:02d}_{time_days:.0f}d.pdf')
        if os.path.isdir(args.outputfile):
            args.outputfile = os.path.join(args.outputfile, defaultoutputfile)
        outfilename = str(args.outputfile).format(timestep=timesteplist[0], time_days=time_days)
    elif len(set(mgilist)) == 1:  # single grid cell plot
        figure_title = f'{modelname}\nCell {mgilist[0]}'

        defaultoutputfile = Path('plotestimators_cell{modelgridindex:03d}.pdf')
        if os.path.isdir(args.outputfile):
            args.outputfile = os.path.join(args.outputfile, defaultoutputfile)
        outfilename = str(args.outputfile).format(modelgridindex=mgilist[0])
    else:  # mix of timesteps and cells somehow?
        figure_title = f'{modelname}'

        defaultoutputfile = Path('plotestimators.pdf')
        if os.path.isdir(args.outputfile):
            args.outputfile = os.path.join(args.outputfile, defaultoutputfile)
        outfilename = args.outputfile

    if not args.notitle:
        axes[0].set_title(figure_title, fontsize=11)
    # plt.suptitle(figure_title, fontsize=11, verticalalignment='top')

    fig.savefig(outfilename, format='pdf')
    print(f'Saved {outfilename}')
    if args.show:
        plt.show()
    else:
        plt.close()


def plot_recombrates(estimators, outfilename, **plotkwargs):
    atomic_number = 28
    ion_stage_list = [2, 3, 4, 5]
    fig, axes = plt.subplots(
        nrows=len(ion_stage_list), ncols=1, sharex=True, figsize=(5, 8),
        tight_layout={"pad": 0.5, "w_pad": 0.0, "h_pad": 0.0})
    # ax.xaxis.set_minor_locator(ticker.MultipleLocator(base=5))

    for ax, ion_stage in zip(axes, ion_stage_list):

        ionstr = f'{at.elsymbols[atomic_number]} {at.roman_numerals[ion_stage]} to {at.roman_numerals[ion_stage - 1]}'

        listT_e = []
        list_rrc = []
        for _, dicttimestepmodelgrid in estimators.items():
            if (atomic_number, ion_stage) in dicttimestepmodelgrid['RRC_LTE_Nahar']:
                listT_e.append(dicttimestepmodelgrid['Te'])
                list_rrc.append(dicttimestepmodelgrid['RRC_LTE_Nahar'][(atomic_number, ion_stage)])

        if not list_rrc:
            continue

        listT_e, list_rrc = zip(*sorted(zip(listT_e, list_rrc), key=lambda x: x[0]))

        rrcfiles = glob.glob(
            f'/Users/lshingles/Library/Mobile Documents/com~apple~CloudDocs/GitHub/artis-atomic/atomic-data-nahar/{at.elsymbols[atomic_number].lower()}{ion_stage - 1}.rrc*.txt')
        if rrcfiles:
            dfrecombrates = get_ionrecombrates_fromfile(rrcfiles[0])

            dfrecombrates.query("logT > @logT_e_min & logT < @logT_e_max",
                                local_dict={'logT_e_min': math.log10(min(listT_e)),
                                            'logT_e_max': math.log10(max(listT_e))}, inplace=True)

            listT_e_Nahar = [10 ** x for x in dfrecombrates['logT'].values]
            ax.plot(listT_e_Nahar, dfrecombrates['RRC_total'], linewidth=2,
                      label=ionstr + " (Nahar)", markersize=6, marker='s', **plotkwargs)

        ax.plot(listT_e, list_rrc, linewidth=2, label=ionstr, markersize=6, marker='s', **plotkwargs)

        ax.legend(loc='best', handlelength=2, frameon=False, numpoints=1, prop={'size': 10})

    # modelname = at.get_model_name(".")
    # plotlabel = f'Timestep {timestep}'
    # time_days = float(at.get_timestep_time('spec.out', timestep))
    # if time_days >= 0:
    #     plotlabel += f' (t={time_days:.2f}d)'
    # fig.suptitle(plotlabel, fontsize=12)

    fig.savefig(outfilename, format='pdf')
    print(f'Saved {outfilename}')
    plt.close()


def addargs(parser):
    parser.add_argument('-modelpath', default='.',
                        help='Path to ARTIS folder')

    parser.add_argument('--recombrates', action='store_true',
                        help='Make a recombination rate plot')

    parser.add_argument('-modelgridindex', '-cell', type=int, default=-1,
                        help='Modelgridindex for time evolution plot')

    parser.add_argument('-timestep', '-ts',
                        help='Timestep number for internal structure plot')

    parser.add_argument('-timedays', '-time', '-t',
                        help='Time in days to plot for internal structure plot')

    parser.add_argument('-x',
                        help='Horizontal axis variable, e.g. cellid, velocity, timestep, or time')

    parser.add_argument('-xmin', type=int, default=-1,
                        help='Plot range: minimum x value')

    parser.add_argument('-xmax', type=int, default=-1,
                        help='Plot range: maximum x value')

    parser.add_argument('--notitle', action='store_true',
                        help='Suppress the top title from the plot')

    parser.add_argument('-plotlist', type=list, default=[],
                        help='Plot list (when calling from Python only)')

    parser.add_argument('-ionpoptype', default='elpop', choices=['absolute', 'totalpop', 'elpop'],
                        help=(
                            'Plot absolutely ion populations, or ion populations as a'
                            ' fraction of total or element population'))

    parser.add_argument('-figscale', type=float, default=1.,
                        help='Scale factor for plot area. 1.0 is for single-column')

    parser.add_argument('-show', action='store_true',
                        help='Show plot before quitting')

    parser.add_argument('-o', action='store', dest='outputfile', type=Path, default=Path(),
                        help='Filename for PDF file')


def main(args=None, argsraw=None, **kwargs):
    if args is None:
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            description='Plot ARTIS estimators.')
        addargs(parser)
        parser.set_defaults(**kwargs)
        args = parser.parse_args(argsraw)

    modelpath = args.modelpath

    if args.timedays:
        if isinstance(args.timedays, str) and '-' in args.timedays:
            timestepmin, timestepmax = [
                at.get_closest_timestep(modelpath, float(timedays))
                for timedays in args.timedays.split('-')]
        else:
            timestep = at.get_closest_timestep(modelpath, args.timedays)
            timestepmin, timestepmax = timestep, timestep
    else:
        if not args.timestep:
            if args.modelgridindex > -1:
                timearray = at.get_timestep_times(modelpath)
                timestepmin = 0
                timestepmax = len(timearray) - 1
            else:
                print('ERROR: A time or timestep must be specified if no cell is specified')
                return -1
        elif '-' in args.timestep:
            timestepmin, timestepmax = [int(nts) for nts in args.timestep.split('-')]
        else:
            timestepmin = int(args.timestep)
            timestepmax = timestepmin

    timestepfilter = timestepmin if timestepmin == timestepmax else -1
    estimators = read_estimators(modelpath, modelgridindex=args.modelgridindex, timestep=timestepfilter)

    if not estimators:
        return -1

    if args.plotlist:
        plotlist = args.plotlist
    else:
        plotlist = [
            [['initabundances', ['Fe', 'Ni_stable', 'Ni_56']]],
            # ['heating_gamma', 'heating_coll', 'heating_bf', 'heating_ff'],
            # ['cooling_adiabatic', 'cooling_coll', 'cooling_fb', 'cooling_ff'],
            # ['heating_gamma/gamma_dep'],
            # ['nne'],
            ['Te', 'TR'],
            # [['populations', ['He I', 'He II', 'He III']]],
            # [['populations', ['C I', 'C II', 'C III', 'C IV', 'C V']]],
            # [['populations', ['O I', 'O II', 'O III', 'O IV']]],
            # [['populations', ['Ne I', 'Ne II', 'Ne III', 'Ne IV', 'Ne V']]],
            # [['populations', ['Si I', 'Si II', 'Si III', 'Si IV', 'Si V']]],
            # [['populations', ['Cr I', 'Cr II', 'Cr III', 'Cr IV', 'Cr V']]],
            # [['populations', ['Fe I', 'Fe II', 'Fe III', 'Fe IV', 'Fe V', 'Fe VI', 'Fe VII', 'Fe VIII']]],
            # [['populations', ['Co I', 'Co II', 'Co III', 'Co IV', 'Co V', 'Co VI', 'Co VII']]],
            # [['populations', ['Ni I', 'Ni II', 'Ni III', 'Ni IV', 'Ni V', 'Ni VI', 'Ni VII']]],
            [['populations', ['Fe II', 'Fe III', 'Co II', 'Co III', 'Ni II', 'Ni III']]],
            # [['populations', ['Fe I', 'Fe II', 'Fe III', 'Fe IV', 'Fe V', 'Ni II']]],
            # [['Alpha_R / RRC_LTE_Nahar', ['Fe II', 'Fe III', 'Fe IV', 'Fe V', 'Ni III']]],
            # [['gamma_NT', ['Fe I', 'Fe II', 'Fe III', 'Fe IV', 'Fe V', 'Ni II']]],
        ]

    if args.recombrates:
        plot_recombrates(estimators, "plotestimators_recombrates.pdf")
    else:
        modeldata, _ = at.get_modeldata(modelpath)
        allnonemptymgilist = [modelgridindex for modelgridindex in modeldata.index
                              if not estimators[(timestepmin, modelgridindex)]['emptycell']]

        if args.modelgridindex > -1:
            # plot time evolution in specific cell
            if not args.x:
                args.x = 'time'
            timesteplist_unfiltered = list(range(timestepmin, timestepmax + 1))
            mgilist = [args.modelgridindex] * len(timesteplist_unfiltered)
            make_plot(modelpath, timesteplist_unfiltered, mgilist, estimators, args.x, plotlist, args)
        else:
            # plot a snapshot at each timestep showing internal structure
            if not args.x:
                args.x = 'velocity'
            for timestep in range(timestepmin, timestepmax + 1):
                timesteplist_unfiltered = [timestep] * len(allnonemptymgilist)  # constant timestep
                make_plot(modelpath, timesteplist_unfiltered, allnonemptymgilist, estimators, args.x, plotlist, args)



if __name__ == "__main__":
    main()
