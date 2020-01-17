#!/usr/bin/env python3
"""Artistools - spectra related functions."""
import argparse
import json
import math
import multiprocessing
from collections import namedtuple
from functools import lru_cache
from functools import partial
from pathlib import Path

import matplotlib.ticker as ticker
import matplotlib.pyplot as plt
import matplotlib as mpl
import numpy as np
import pandas as pd
from astropy import constants as const
from astropy import units as u
from scipy import interpolate

import artistools as at
import artistools.packets

EMTYPECOLUMN = 'emissiontype'
# EMTYPECOLUMN = 'trueemissiontype'


@at.diskcache(quiet=True)
def get_packets_with_emtype_onefile(lineindices, packetsfile):
    dfpackets = at.packets.readfile(packetsfile, usecols=[
        'type_id', 'e_cmf', 'e_rf', 'nu_rf', 'escape_type_id', 'escape_time',
        'em_posx', 'em_posy', 'em_posz', 'em_time',
        'posx', 'posy', 'posz', 'dirx', 'diry', 'dirz', 'emissiontype',
        'trueemissiontype', 'true_emission_velocity'],
        type='TYPE_ESCAPE', escape_type='TYPE_RPKT')

    dfpackets_selected = dfpackets.query(f'{EMTYPECOLUMN} in @lineindices', inplace=False)

    return dfpackets_selected


@lru_cache(maxsize=16)
@at.diskcache(savegzipped=True)
def get_packets_with_emtype(modelpath, lineindices, maxpacketfiles=None):
    packetsfiles = at.packets.get_packetsfilepaths(modelpath, maxpacketfiles=maxpacketfiles)
    nprocs_read = len(packetsfiles)
    assert nprocs_read > 0

    model, _ = at.get_modeldata(modelpath)
    # vmax = model.iloc[-1].velocity_outer * u.km / u.s
    processfile = partial(get_packets_with_emtype_onefile, lineindices)
    if at.enable_multiprocessing:
        with multiprocessing.get_context("spawn").Pool() as pool:
            arr_dfmatchingpackets = pool.map(processfile, packetsfiles)
            pool.close()
            pool.join()
    else:
        arr_dfmatchingpackets = [processfile(f) for f in packetsfiles]

    dfmatchingpackets = pd.concat(arr_dfmatchingpackets)

    return dfmatchingpackets, nprocs_read


def calculate_timebinned_packet_sum(dfpackets, timearrayplusend):
    binned = pd.cut(dfpackets['t_arrive_d'], timearrayplusend, labels=False, include_lowest=True)

    binnedenergysums = np.zeros_like(timearrayplusend[:-1], dtype=np.float)
    for binindex, e_rf_sum in dfpackets.groupby(binned)['e_rf'].sum().iteritems():
        binnedenergysums[int(binindex)] = e_rf_sum

    return binnedenergysums


def get_line_fluxes_from_packets(emfeatures, modelpath, maxpacketfiles=None, arr_tstart=None, arr_tend=None):
    if arr_tstart is None:
        arr_tstart = at.get_timestep_times_float(modelpath, loc='start')
    if arr_tend is None:
        arr_tend = at.get_timestep_times_float(modelpath, loc='end')

    arr_timedelta = np.array(arr_tend) - np.array(arr_tstart)
    arr_tmid = (np.array(arr_tstart) + np.array(arr_tend)) / 2.

    model, _ = at.get_modeldata(modelpath)
    # vmax = model.iloc[-1].velocity_outer * u.km / u.s
    # betafactor = math.sqrt(1 - (vmax / const.c).decompose().value ** 2)

    timearrayplusend = np.concatenate([arr_tstart, [arr_tend[-1]]])

    dictlcdata = {'time': arr_tmid}

    for feature in emfeatures:
        # dictlcdata[feature.colname] = np.zeros_like(arr_tstart, dtype=np.float)

        dfpackets_selected, nprocs_read = get_packets_with_emtype(
            modelpath, feature.linelistindices, maxpacketfiles=maxpacketfiles)

        normfactor = (1. / 4 / math.pi / (u.megaparsec.to('cm') ** 2) / nprocs_read / u.s.to('day'))

        energysumsreduced = calculate_timebinned_packet_sum(dfpackets_selected, timearrayplusend)
        fluxdata = np.divide(energysumsreduced * normfactor, arr_timedelta)
        dictlcdata[feature.colname] = fluxdata

    lcdata = pd.DataFrame(dictlcdata)
    return lcdata


def get_closelines(modelpath, atomic_number, ion_stage, approxlambda, lambdamin, lambdamax):
    dflinelist = at.get_linelist(modelpath, returntype='dataframe')
    dflinelistclosematches = dflinelist.query(
        'atomic_number == @atomic_number and ionstage == @ion_stage and @lambdamin < lambda_angstroms < @lambdamax')

    linelistindices = tuple(dflinelistclosematches.index.values)
    lowestlambda = dflinelistclosematches.lambda_angstroms.min()
    highestlamba = dflinelistclosematches.lambda_angstroms.max()
    colname = f'flux_{at.get_ionstring(atomic_number, ion_stage, nospace=True)}_{approxlambda}'
    featurelabel = f'{at.get_ionstring(atomic_number, ion_stage)} {approxlambda} Å'

    return (colname, featurelabel, approxlambda, linelistindices, lowestlambda, highestlamba, atomic_number, ion_stage)


def get_labelandlineindices(modelpath, emfeaturesearch):
    featuretuple = namedtuple('feature', [
        'colname', 'featurelabel', 'approxlambda', 'linelistindices', 'lowestlambda',
        'highestlamba', 'atomic_number', 'ion_stage'])

    labelandlineindices = []
    for params in emfeaturesearch:
        feature = featuretuple(*get_closelines(modelpath, *params))
        print(f'{feature.featurelabel} includes {len(feature.linelistindices)} lines '
              f'[{feature.lowestlambda:.1f} Å, {feature.highestlamba:.1f} Å]')
        labelandlineindices.append(feature)
    # labelandlineindices.append(featuretuple(*get_closelines(dflinelist, 26, 2, 7155, 7150, 7160)))
    # labelandlineindices.append(featuretuple(*get_closelines(dflinelist, 26, 2, 12570, 12470, 12670)))
    # labelandlineindices.append(featuretuple(*get_closelines(dflinelist, 28, 2, 7378, 7373, 7383)))

    return labelandlineindices


def make_flux_ratio_plot(args):
    # font = {'size': 16}
    # matplotlib.rc('font', **font)
    nrows = 1
    fig, axes = plt.subplots(
        nrows=nrows, ncols=1, sharey=False,
        figsize=(args.figscale * at.figwidth, args.figscale * at.figwidth * (0.25 + nrows * 0.4)),
        tight_layout={"pad": 0.2, "w_pad": 0.0, "h_pad": 0.0})

    if nrows == 1:
        axes = [axes]

    axis = axes[0]
    axis.set_yscale('log')
    # axis.set_ylabel(r'log$_1$$_0$ F$_\lambda$ at 1 Mpc [erg/s/cm$^2$/$\mathrm{{\AA}}$]')

    # axis.set_xlim(left=supxmin, right=supxmax)
    pd.set_option('display.max_rows', 3500)
    pd.set_option('display.width', 150)
    pd.options.display.max_rows = 500
    for seriesindex, (modelpath, modellabel, modelcolor) in enumerate(zip(args.modelpath, args.label, args.color)):
        print(f"====> {modellabel}")

        emfeatures = get_labelandlineindices(modelpath, tuple(args.emfeaturesearch))

        dflcdata = get_line_fluxes_from_packets(emfeatures, modelpath, maxpacketfiles=args.maxpacketfiles,
                                                arr_tstart=args.timebins_tstart,
                                                arr_tend=args.timebins_tend)

        dflcdata.eval(f'fratio = {emfeatures[1].colname} / {emfeatures[0].colname}', inplace=True)
        axis.set_ylabel(r'F$_{\mathrm{' + emfeatures[1].featurelabel + r'}}$ / F$_{\mathrm{' +
                        emfeatures[0].featurelabel + r'}}$')
        # \mathrm{\AA}

        # for row in dflcdata
        # print(dflcdata.time)
        # print(dflcdata)

        axis.plot(dflcdata.time, dflcdata['fratio'], label=modellabel, marker='s', color=modelcolor)

        tmin = dflcdata.time.min()
        tmax = dflcdata.time.max()

    if args.emfeaturesearch[0][:3] == (26, 2, 7155) and args.emfeaturesearch[1][:3] == (26, 2, 12570):
        axis.set_ylim(ymin=0.05)
        axis.set_ylim(ymax=4.2)
        arr_tdays = np.linspace(tmin, tmax, 3)
        arr_floersfit = [10 ** (0.0043 * timedays - 1.65) for timedays in arr_tdays]
        for ax in axes:
            ax.plot(arr_tdays, arr_floersfit, color='black', label='Floers et al. (2019) best fit', lw=2.)

    m18_tdays = np.array([206, 229, 303, 339])
    m18_pew = {}
    # m18_pew[(26, 2, 12570)] = np.array([2383, 1941, 2798, 6770])
    m18_pew[(26, 2, 7155)] = np.array([618, 417, 406, 474])
    m18_pew[(28, 2, 7378)] = np.array([157, 256, 236, 309])
    if args.emfeaturesearch[1][:3] in m18_pew and args.emfeaturesearch[0][:3] in m18_pew:
        axis.set_ylim(ymax=12)
        arr_fratio = m18_pew[args.emfeaturesearch[1][:3]] / m18_pew[args.emfeaturesearch[0][:3]]
        for ax in axes:
            ax.plot(m18_tdays, arr_fratio, color='black', label='Maguire et al. (2018)', lw=2., marker='s')

    for ax in axes:
        ax.set_xlabel(r'Time [days]')
        ax.tick_params(which='both', direction='in')
        ax.legend(loc='upper right', frameon=False, handlelength=1, ncol=2, numpoints=1)

    defaultoutputfile = 'linefluxes.pdf'
    if not args.outputfile:
        args.outputfile = defaultoutputfile
    elif not Path(args.outputfile).suffixes:
        args.outputfile = args.outputfile / defaultoutputfile

    fig.savefig(args.outputfile, format='pdf')
    # plt.show()
    print(f'Saved {args.outputfile}')
    plt.close()


@at.diskcache()
def get_packets_with_emission_conditions(modelpath, lineindices, tstart, tend, maxpacketfiles=None):
    estimators = at.estimators.read_estimators(modelpath, get_ion_values=False, get_heatingcooling=False)

    modeldata, _ = at.get_modeldata(modelpath)
    allnonemptymgilist = [modelgridindex for modelgridindex in modeldata.index
                          if not estimators[(0, modelgridindex)]['emptycell']]

    # model_tmids = at.get_timestep_times_float(modelpath, loc='mid')
    # arr_velocity_mid = tuple(list([(float(v1) + float(v2)) * 0.5 for v1, v2 in zip(
    #     modeldata['velocity_inner'].values, modeldata['velocity_outer'].values)]))

    # interp_log10nne, interp_te = {}, {}
    # for ts in range(len(model_tmids)):
    #     arr_v = np.zeros_like(allnonemptymgilist, dtype='float')
    #     arr_log10nne = np.zeros_like(allnonemptymgilist, dtype='float')
    #     arr_te = np.zeros_like(allnonemptymgilist, dtype='float')
    #     for i, mgi in enumerate(allnonemptymgilist):
    #         arr_v[i] = arr_velocity_mid[mgi]
    #         arr_log10nne[i] = math.log10(float(estimators[(ts, mgi)]['nne']))
    #         arr_te[i] = estimators[(ts, mgi)]['Te']
    #
    #     interp_log10nne[ts] = interpolate.interp1d(arr_v.copy(), arr_log10nne.copy(),
    #                                                kind='linear', fill_value='extrapolate')
    #     interp_te[ts] = interpolate.interp1d(arr_v.copy(), arr_te.copy(), kind='linear', fill_value='extrapolate')

    dfpackets_selected, _ = get_packets_with_emtype(
        modelpath, lineindices, maxpacketfiles=maxpacketfiles)

    dfpackets_selected = dfpackets_selected.query(
        't_arrive_d >= @tstart and t_arrive_d <= @tend', inplace=False).copy()

    dfpackets_selected = at.packets.add_derived_columns(
        dfpackets_selected, modelpath, ['em_timestep', 'em_modelgridindex'],
        allnonemptymgilist=allnonemptymgilist)

    if not dfpackets_selected.empty:
        def em_lognne(packet):
            # return interp_log10nne[packet.em_timestep](packet.true_emission_velocity)
            return math.log10(estimators[(packet.em_timestep, packet.em_modelgridindex)]['nne'])

        dfpackets_selected['em_log10nne'] = dfpackets_selected.apply(em_lognne, axis=1)

        def em_Te(packet):
            # return interp_te[packet.em_timestep](packet.true_emission_velocity)
            return estimators[(packet.em_timestep, packet.em_modelgridindex)]['Te']

        dfpackets_selected['em_Te'] = dfpackets_selected.apply(em_Te, axis=1)

    return dfpackets_selected


def plot_nne_te_points(axis, serieslabel, em_log10nne, em_Te, normtotalpackets, color, marker='o'):
    # color_adj = [(c + 0.3) / 1.3 for c in mpl.colors.to_rgb(color)]
    color_adj = [(c + 0.1) / 1.1 for c in mpl.colors.to_rgb(color)]
    hitcount = {}
    for log10nne, Te in zip(em_log10nne, em_Te):
        hitcount[(log10nne, Te)] = hitcount.get((log10nne, Te), 0) + 1

    if hitcount:
        arr_log10nne, arr_te = zip(*hitcount.keys())
    else:
        arr_log10nne, arr_te = np.array([]), np.array([])

    arr_weight = np.array([hitcount[(x, y)] for x, y in zip(arr_log10nne, arr_te)])
    arr_weight = (arr_weight / normtotalpackets) * 500
    arr_size = np.sqrt(arr_weight) * 10

    # arr_weight = arr_weight / float(max(arr_weight))
    # arr_color = np.zeros((len(arr_x), 4))
    # arr_color[:, :3] = np.array([[c for c in mpl.colors.to_rgb(color)] for x in arr_weight])
    # arr_color[:, 3] = (arr_weight + 0.2) / 1.2
    # np.array([[c * z for c in mpl.colors.to_rgb(color)] for z in arr_z])

    # axis.scatter(arr_log10nne, arr_te, s=arr_weight * 20, marker=marker, color=color_adj, lw=0, alpha=1.0,
    #              edgecolors='none')
    alpha = 0.8
    axis.scatter(arr_log10nne, arr_te, s=arr_size, marker=marker, color=color_adj, lw=0, alpha=alpha,
                 edgecolors='none')

    # make an invisible plot series to appear in the legend with a fixed marker size
    axis.plot([0], [0], marker=marker, markersize=3, color=color_adj, linestyle='None', label=serieslabel, alpha=alpha)

    # axis.plot(em_log10nne, em_Te, label=serieslabel, linestyle='None',
    #           marker='o', markersize=2.5, markeredgewidth=0, alpha=0.05,
    #           fillstyle='full', color=color_b)


def plot_nne_te_bars(axis, serieslabel, em_log10nne, em_Te, color):
    if len(em_log10nne) == 0:
        return
    errorbarkwargs = dict(xerr=np.std(em_log10nne), yerr=np.std(em_Te),
                          color='black', markersize=10., fillstyle='full',
                          capthick=4, capsize=15, linewidth=4.,
                          alpha=1.0)
    # black larger one for an outline
    axis.errorbar(np.mean(em_log10nne), np.mean(em_Te), **errorbarkwargs)
    errorbarkwargs['markersize'] -= 2
    errorbarkwargs['capthick'] -= 2
    errorbarkwargs['capsize'] -= 1
    errorbarkwargs['linewidth'] -= 2
    errorbarkwargs['color'] = color
    # errorbarkwargs['zorder'] += 0.5
    axis.errorbar(np.mean(em_log10nne), np.mean(em_Te), **errorbarkwargs)


def make_emitting_regions_plot(args):

    # font = {'size': 16}
    # matplotlib.rc('font', **font)

    with open('floers_te_nne.json', encoding='utf-8') as data_file:
        floers_te_nne = json.loads(data_file.read())

    # give an ordering and index to dict items
    floers_keys = [t for t in sorted(floers_te_nne.keys(), key=lambda x: float(x))]  # strings, not floats
    floers_times = np.array([float(t) for t in floers_keys])
    floers_data = [floers_te_nne[t] for t in floers_keys]
    print(f'Floers data available for times: {list(floers_times)}')

    times_days = (np.array(args.timebins_tstart) + np.array(args.timebins_tend)) / 2.

    print(f'Chosen times: {times_days}')

    # axis.set_xlim(left=supxmin, right=supxmax)
    # pd.set_option('display.max_rows', 50)
    pd.set_option('display.width', 250)
    pd.options.display.max_rows = 500

    emdata_all = {}

    # data is collected, now make plots
    defaultoutputfile = 'emittingregions.pdf'
    if not args.outputfile:
        args.outputfile = defaultoutputfile
    elif not Path(args.outputfile).suffixes:
        args.outputfile = args.outputfile / defaultoutputfile

    args.modelpath.append(None)
    args.label.append(f'All models: {args.label}')
    args.modeltag.append('all')
    for modelindex, (modelpath, modellabel, modeltag) in enumerate(
            zip(args.modelpath, args.label, args.modeltag)):

        print(f"ARTIS model: '{modellabel}'")

        if modelpath is not None:
            print(f"Getting packets/nne/Te data for ARTIS model: '{modellabel}'")

            emdata_all[modelindex] = {}

            emfeatures = get_labelandlineindices(modelpath, tuple(args.emfeaturesearch))

            for feature in emfeatures:
                for tmid, tstart, tend in zip(times_days, args.timebins_tstart, args.timebins_tend):

                    dfpackets = get_packets_with_emission_conditions(
                        modelpath, feature.linelistindices, tstart, tend, maxpacketfiles=args.maxpacketfiles)

                    dfpackets_selected = dfpackets.query(f'{EMTYPECOLUMN} in @feature.linelistindices', inplace=False)
                    if dfpackets_selected.empty:
                        emdata_all[modelindex][(tmid, feature.colname)] = {
                            'em_log10nne': [],
                            'em_Te': []}
                    else:
                        emdata_all[modelindex][(tmid, feature.colname)] = {
                            'em_log10nne': dfpackets_selected.em_log10nne.values,
                            'em_Te': dfpackets_selected.em_Te.values}

        for timeindex, tmid in enumerate(times_days):
            print(f'  Plot at {tmid} days')

            nrows = 1
            fig, axis = plt.subplots(
                nrows=nrows, ncols=1, sharey=False, sharex=False,
                figsize=(args.figscale * at.figwidth, args.figscale * at.figwidth * (0.25 + nrows * 0.7)),
                tight_layout={"pad": 0.2, "w_pad": 0.0, "h_pad": 0.2})

            floersindex = np.abs(floers_times - tmid).argmin()
            axis.plot(floers_data[floersindex]['ne'], floers_data[floersindex]['temp'],
                      color='black', lw=2, label=f'Floers et al. (2019) {floers_keys[floersindex]}d')

            if modeltag == 'all':
                for bars in [False, True]:
                    for truemodelindex in range(modelindex):
                        emfeatures = get_labelandlineindices(args.modelpath[truemodelindex], args.emfeaturesearch)

                        em_log10nne = np.concatenate(
                            [emdata_all[truemodelindex][(tmid, feature.colname)]['em_log10nne']
                             for feature in emfeatures])

                        em_Te = np.concatenate(
                            [emdata_all[truemodelindex][(tmid, feature.colname)]['em_Te']
                             for feature in emfeatures])

                        normtotalpackets = len(em_log10nne) * 8.  # circles have more area than triangles, so decrease
                        modelcolor = args.color[truemodelindex]
                        if not bars:
                            plot_nne_te_points(
                                axis, args.label[truemodelindex], em_log10nne, em_Te, normtotalpackets, modelcolor)
                        else:
                            plot_nne_te_bars(axis, args.label[truemodelindex], em_log10nne, em_Te, modelcolor)
            else:
                modellabel = args.label[modelindex]
                emfeatures = get_labelandlineindices(modelpath, tuple(args.emfeaturesearch))

                featurecolours = ['blue', 'red']
                markers = [10, 11]
                # featurecolours = ['C0', 'C3']
                # featurebarcolours = ['blue', 'red']

                normtotalpackets = np.sum([len(emdata_all[modelindex][(tmid, feature.colname)]['em_log10nne'])
                                           for feature in emfeatures])

                for bars in [False, True]:
                    for featureindex, feature in enumerate(emfeatures):
                        emdata = emdata_all[modelindex][(tmid, feature.colname)]

                        if not bars:
                            print(f'   {len(emdata["em_log10nne"])} points plotted for {feature.featurelabel}')
                        serieslabel = modellabel + ' ' + feature.featurelabel.replace('Å', r' $\mathrm{\AA}$')
                        if not bars:
                            plot_nne_te_points(
                                axis, serieslabel, emdata['em_log10nne'], emdata['em_Te'],
                                normtotalpackets, featurecolours[featureindex], marker=markers[featureindex])
                        else:
                            plot_nne_te_bars(
                                axis, serieslabel, emdata['em_log10nne'], emdata['em_Te'], featurecolours[featureindex])

            axis.legend(loc='upper left', frameon=False, handlelength=1, ncol=1,
                        numpoints=1, fontsize='small', markerscale=3.)

            axis.set_ylim(ymin=3000)
            axis.set_ylim(ymax=10000)
            axis.set_xlim(xmin=5., xmax=7.)

            axis.tick_params(which='both', direction='in')
            axis.set_xlabel(r'log$_{10}$(n$_{\mathrm{e}}$ [cm$^{-3}$])')
            axis.set_ylabel(r'Electron Temperature [K]')

            axis.annotate(f'{tmid:.0f}d', xy=(0.98, 0.96), xycoords='axes fraction',
                          horizontalalignment='right', verticalalignment='top', fontsize=16)

            outputfile = str(args.outputfile).format(timeavg=tmid, modeltag=modeltag)
            fig.savefig(outputfile, format='pdf')
            print(f'    Saved {outputfile}')
            plt.close()


def addargs(parser):
    parser.add_argument('-modelpath', default=[], nargs='*', action=at.AppendPath,
                        help='Paths to ARTIS folders with spec.out or packets files')

    parser.add_argument('-label', default=[], nargs='*',
                        help='List of series label overrides')

    parser.add_argument('-modeltag', default=[], nargs='*',
                        help='List of model tags for file names')

    parser.add_argument('-color', default=[f'C{i}' for i in range(10)], nargs='*',
                        help='List of line colors')

    parser.add_argument('-linestyle', default=[], nargs='*',
                        help='List of line styles')

    parser.add_argument('-linewidth', default=[], nargs='*',
                        help='List of line widths')

    parser.add_argument('-dashes', default=[], nargs='*',
                        help='Dashes property of lines')

    parser.add_argument('-maxpacketfiles', type=int, default=None,
                        help='Limit the number of packet files read')

    parser.add_argument('-emfeaturesearch', default=[], nargs='*',
                        help='List of tuples (TODO explain)')

    # parser.add_argument('-timemin', type=float,
    #                     help='Lower time in days to integrate spectrum')
    #
    # parser.add_argument('-timemax', type=float,
    #                     help='Upper time in days to integrate spectrum')
    #
    parser.add_argument('-xmin', type=int, default=50,
                        help='Plot range: minimum wavelength in Angstroms')

    parser.add_argument('-xmax', type=int, default=450,
                        help='Plot range: maximum wavelength in Angstroms')

    parser.add_argument('-ymin', type=float, default=None,
                        help='Plot range: y-axis')

    parser.add_argument('-ymax', type=float, default=None,
                        help='Plot range: y-axis')

    parser.add_argument('-timebins_tstart', default=[], nargs='*', action='append',
                        help='Time bin start values in days')

    parser.add_argument('-timebins_tend', default=[], nargs='*', action='append',
                        help='Time bin end values in days')

    parser.add_argument('-figscale', type=float, default=1.8,
                        help='Scale factor for plot area. 1.0 is for single-column')

    parser.add_argument('--write_data', action='store_true',
                        help='Save data used to generate the plot in a CSV file')

    parser.add_argument('--plotemittingregions', action='store_true',
                        help='Plot conditions where flux line is emitted')

    parser.add_argument('-outputfile', '-o', action='store', dest='outputfile', type=Path,
                        help='path/filename for PDF file')


def main(args=None, argsraw=None, **kwargs):
    """Plot spectra from ARTIS and reference data."""
    if args is None:
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            description='Plot ARTIS model spectra by finding spec.out files '
                        'in the current directory or subdirectories.')
        addargs(parser)
        parser.set_defaults(**kwargs)
        args = parser.parse_args(argsraw)

    if not args.modelpath:
        args.modelpath = [Path('.')]
    elif isinstance(args.modelpath, (str, Path)):
        args.modelpath = [args.modelpath]

    args.modelpath = at.flatten_list(args.modelpath)

    args.label, args.modeltag, args.color = at.trim_or_pad(len(args.modelpath), args.label, args.modeltag, args.color)

    for i in range(len(args.label)):
        if args.label[i] is None:
            args.label[i] = at.get_model_name(args.modelpath[i])

    if args.plotemittingregions:
        make_emitting_regions_plot(args)
    else:
        make_flux_ratio_plot(args)


if __name__ == "__main__":
    main()