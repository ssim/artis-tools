#!/usr/bin/env python3
import argparse
import math
import multiprocessing
import os
import sys

import matplotlib.ticker as ticker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from astropy import constants as const
from scipy import linalg
from pathlib import Path
# from bigfloat import *
from math import atan
# import numba
# from numpy import arctan as atan

import artistools as at
import artistools.estimators
import artistools.nltepops
import artistools.nonthermal


minionfraction = 1.e-8  # minimum number fraction of the total population to include in SF solution

defaultoutputfile = 'spencerfano_cell{cell:03d}_ts{timestep:02d}_{timedays:.0f}d.pdf'

# cgs units to match artis
EV = 1.6021772e-12  # in erg
H = 6.6260755e-27
ME = 9.1093897e-28
QE = 4.80325E-10
H_ionpot = 13.5979996 * EV
CLIGHT = 2.99792458e+10
PI = math.pi


def get_lte_pops(adata, ions, ionpopdict, temperature):
    poplist = []

    K_B = const.k_B.to('eV / K').value

    for _, ion in adata.iterrows():
        ionid = (ion.Z, ion.ion_stage)
        if ionid in ions:
            Z = ion.Z
            ionstage = ion.ion_stage
            nnion = ionpopdict[(Z, ionstage)]

            ltepartfunc = ion.levels.eval('g * exp(-energy_ev / @K_B / @temperature)').sum()

            for levelindex, level in ion.levels.iterrows():
                nnlevel = nnion / ltepartfunc * math.exp(-level.energy_ev / K_B / temperature)

                poprow = (Z, ionstage, levelindex, nnlevel, nnlevel, nnlevel / nnion)
                poplist.append(poprow)

    dfpop = pd.DataFrame(poplist, columns=['Z', 'ion_stage', 'level', 'n_LTE', 'n_NLTE', 'ion_popfrac'])
    return dfpop


def read_binding_energies(modelpath=None):
    if modelpath:
        collionfilename = os.path.join(modelpath, 'binding_energies.txt')
    else:
        collionfilename = os.path.join(at.PYDIR, 'data', 'binding_energies.txt')

    with open(collionfilename, "r") as f:
        nt_shells, n_z_binding = [int(x) for x in f.readline().split()]
        electron_binding = np.zeros((n_z_binding, nt_shells))

        for i in range(n_z_binding):
            electron_binding[i] = np.array([float(x) for x in f.readline().split()]) * EV

    return electron_binding


def get_electronoccupancy(atomic_number, ion_stage, nt_shells):
    q = np.zeros(nt_shells)

    ioncharge = ion_stage - 1
    nbound = atomic_number - ioncharge  # number of bound electrons

    for electron_loop in range(nbound):
        if q[0] < 2:  # K 1s
            q[0] += 1
        elif(q[1] < 2):  # L1 2s
            q[1] += 1
        elif(q[2] < 2):  # L2 2p[1/2]
            q[2] += 1
        elif(q[3] < 4):  # L3 2p[3/2]
            q[3] += 1
        elif(q[4] < 2):  # M1 3s
            q[4] += 1
        elif(q[5] < 2):  # M2 3p[1/2]
            q[5] += 1
        elif(q[6] < 4):  # M3 3p[3/2]
            q[6] += 1
        elif ioncharge == 0:
            if q[9] < 2:  # N1 4s
                q[9] += 1
            elif q[7] < 4:  # M4 3d[3/2]
                q[7] += 1
            elif q[8] < 6:  # M5 3d[5/2]
                q[8] += 1
            else:
                print("Going beyond the 4s shell in NT calculation. Abort!\n")
        elif ioncharge == 1:
            if q[9] < 1:  # N1 4s
                q[9] += 1
            elif q[7] < 4:  # M4 3d[3/2]
                q[7] += 1
            elif q[8] < 6:  # M5 3d[5/2]
                q[8] += 1
            else:
                print("Going beyond the 4s shell in NT calculation. Abort!\n")
        elif(ioncharge > 1):
            if q[7] < 4:  # M4 3d[3/2]
                q[7] += 1
            elif q[8] < 6:  # M5 3d[5/2]
                q[8] += 1
            else:
                print("Going beyond the 4s shell in NT calculation. Abort!\n")
    return q


def get_mean_binding_energy(atomic_number, ion_stage, electron_binding, ionpot_ev):
    n_z_binding, nt_shells = electron_binding.shape
    q = get_electronoccupancy(atomic_number, ion_stage, nt_shells)

    total = 0.0
    for electron_loop in range(nt_shells):
        electronsinshell = q[electron_loop]
        if ((electronsinshell) > 0):
            use2 = electron_binding[atomic_number - 1][electron_loop]
            use3 = ionpot_ev * EV
        if (use2 <= 0):
            use2 = electron_binding[atomic_number - 1][electron_loop-1]
            # to get total += electronsinshell/electron_binding[get_element(element)-1][electron_loop-1];
            # set use3 = 0.
            if (electron_loop != 8):
                # For some reason in the Lotz data, this is no energy for the M5 shell before Ni. So if the complaint
                # is for 8 (corresponding to that shell) then just use the M4 value
                print(f"Huh? I'm trying to use a binding energy when I have no data. element {atomic_number} ionstage {ion_stage}\n")
                assert(electron_loop == 8)
                # print("Z = %d, ion_stage = %d\n", get_element(element), get_ionstage(element, ion));
        if (use2 < use3):
            total += electronsinshell / use3
        else:
            total += electronsinshell / use2
        # print("total total)

    return total


def get_lotz_xs_ionisation(atomic_number, ion_stage, electron_binding, ionpot_ev, en_ev):
    # Axelrod 1980 Eq 3.38

    en_erg = en_ev * EV
    gamma = en_erg / (ME * CLIGHT ** 2) + 1
    beta = math.sqrt(1. - 1. / (gamma ** 2))
    # beta = 0.99
    # print(f'{gamma=} {beta=}')

    n_z_binding, nt_shells = electron_binding.shape
    q = get_electronoccupancy(atomic_number, ion_stage, nt_shells)

    part_sigma = 0.0
    for electron_loop in range(nt_shells):
        electronsinshell = q[electron_loop]
        if ((electronsinshell) > 0):
            use2 = electron_binding[atomic_number - 1][electron_loop]
            use3 = ionpot_ev * EV
        if (use2 <= 0):
            use2 = electron_binding[atomic_number - 1][electron_loop-1]
            # to get total += electronsinshell/electron_binding[get_element(element)-1][electron_loop-1];
            # set use3 = 0.
            if (electron_loop != 8):
                # For some reason in the Lotz data, this is no energy for the M5 shell before Ni. So if the complaint
                # is for 8 (corresponding to that shell) then just use the M4 value
                print(f"Huh? I'm trying to use a binding energy when I have no data. element {atomic_number} ionstage {ion_stage}\n")
                assert(electron_loop == 8)
                # print("Z = %d, ion_stage = %d\n", get_element(element), get_ionstage(element, ion));

        if (use2 < use3):
            p = use3
        else:
            p = use2

        if 0.5 * beta ** 2 * ME * CLIGHT ** 2 > p:
            part_sigma += electronsinshell / p * (
                (math.log(beta ** 2 * ME * CLIGHT ** 2 / 2. / p) - math.log10(1 - beta ** 2) - beta ** 2))

    Aconst = 1.33e-14 * EV * EV
    # me is electron mass
    sigma = 2 * Aconst / (beta ** 2) / ME / (CLIGHT ** 2) * part_sigma
    assert(sigma >= 0)
    return sigma


def lossfunction(energy_ev, nne_cgs, nnetot_cgs, use_nnetot=False):
    # return math.log(energy_ev) / energy_ev
    nne_selected_cgs = nnetot_cgs if use_nnetot else nne_cgs

    nne = nne_selected_cgs   # convert from cm^-3 to m^-3
    energy = energy_ev * EV  # convert eV to J

    # omegap = math.sqrt(4 * math.pi * nne_selected_cgs * pow(QE, 2) / ME)
    omegap = 5.64e4 * math.sqrt(nne_selected_cgs)
    zetae = H * omegap / 2 / math.pi

    if energy_ev > 14:
        assert 2 * energy > zetae
        lossfunc = nne * 2 * math.pi * QE ** 4 / energy * math.log(2 * energy / zetae)
    else:
        v = math.sqrt(2 * energy / ME)  # velocity in m/s
        eulergamma = 0.577215664901532
        lossfunc = nne * 2 * math.pi * QE ** 4 / energy * math.log(ME * pow(v, 3) / (eulergamma * pow(QE, 2) * omegap))

    # lossfunc is now in erg / cm
    return lossfunc / EV  # eV / cm


def Psecondary(e_p, ionpot_ev, J, e_s=-1, epsilon=-1):
    assert e_s >= 0 or epsilon >= 0
    # if e_p < I:
    #     return 0.
    #
    if e_s < 0:
        e_s = epsilon - ionpot_ev
    if epsilon < 0:
        epsilon = e_s + ionpot_ev

    #
    # if epsilon < I:
    #     return 0.
    # if e_s < 0:
    #     return 0.
    # if e_s > e_p - I:
    #     return 0.
    # if e_s > e_p:
    #     return 0.

    # test case: constant, always below ionisation
    # Psecondary_e_s_max = 1. / J / 2.
    # return 1. / Psecondary_e_s_max if (e_s < Psecondary_e_s_max) else 0.

    return 1. / J / atan((e_p - ionpot_ev) / 2. / J) / (1 + ((e_s / J) ** 2))


def get_J(Z, ionstage, ionpot_ev):
    # returns an energy in eV
    # values from Opal et al. 1971 as applied by Kozma & Fransson 1992
    if (ionstage == 1):
        if (Z == 2):  # He I
            return 15.8
        elif (Z == 10):  # Ne I
            return 24.2
        elif (Z == 18):  # Ar I
            return 10.0

    return 0.6 * ionpot_ev


def get_xs_excitation_vector(engrid, row):
    A_naught_squared = 2.800285203e-17  # Bohr radius squared in cm^2
    deltaen = engrid[1] - engrid[0]
    npts = len(engrid)
    xs_excitation_vec = np.empty(npts)

    coll_str = row.collstr
    epsilon_trans = row.epsilon_trans_ev * EV
    epsilon_trans_ev = row.epsilon_trans_ev

    startindex = math.ceil((epsilon_trans_ev - engrid[0]) / deltaen)
    xs_excitation_vec[:startindex] = 0.

    if (coll_str >= 0):
        # collision strength is available, so use it
        # Li et al. 2012 equation 11
        constantfactor = pow(H_ionpot, 2) / row.lower_g * coll_str * math.pi * A_naught_squared

        xs_excitation_vec[startindex:] = constantfactor * (engrid[startindex:] * EV) ** -2

    elif not row.forbidden:

        nu_trans = epsilon_trans / H
        g = row.upper_g / row.lower_g
        fij = g * ME * pow(CLIGHT, 3) / (8 * pow(QE * nu_trans * math.pi, 2)) * row.A
        # permitted E1 electric dipole transitions

        g_bar = 0.2

        A = 0.28
        B = 0.15

        prefactor = 45.585750051
        # Eq 4 of Mewe 1972, possibly from Seaton 1962?
        constantfactor = prefactor * A_naught_squared * pow(H_ionpot / epsilon_trans, 2) * fij

        U = engrid[startindex:] / epsilon_trans_ev
        g_bar = A * np.log(U) + B

        xs_excitation_vec[startindex:] = constantfactor * g_bar / U
        for j, energy_ev in enumerate(engrid):
            energy = energy_ev * EV
            if (energy >= epsilon_trans):
                U = energy / epsilon_trans
                g_bar = A * math.log(U) + B
                xs_excitation_vec[j] = constantfactor * g_bar / U
    else:
        xs_excitation_vec[startindex:] = 0.

    return xs_excitation_vec


def calculate_nt_frac_excitation(engrid, dftransitions, yvec, deposition_density_ev):
    # Kozma & Fransson equation 4, but summed over all transitions for given ion
    # integral in Kozma & Fransson equation 9
    deltaen = engrid[1] - engrid[0]
    npts = len(engrid)

    xs_excitation_vec_sum_alltrans = np.zeros(npts)

    for _, row in dftransitions.iterrows():
        nnlevel = row.lower_pop
        xs_excitation_vec_sum_alltrans += nnlevel * row.epsilon_trans_ev * get_xs_excitation_vector(engrid, row)

    return np.dot(xs_excitation_vec_sum_alltrans, yvec) * deltaen / deposition_density_ev


def get_nntot(ions, ionpopdict):
    nntot = 0.
    for Z, ionstage in ions:
        nntot += ionpopdict[(Z, ionstage)]
    return nntot


def get_Zbar(ions, ionpopdict):
    Zbar = 0.  # electrons per ion
    nntot = get_nntot(ions, ionpopdict)
    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        Zbar += Z * nnion / nntot

    return Zbar


def get_Zboundbar(ions, ionpopdict):
    Zboundbar = 0.  # electrons per ion
    nntot = get_nntot(ions, ionpopdict)
    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        Zboundbar += (Z - ionstage + 1) * nnion / nntot

    return Zboundbar


def get_nnetot(ions, ionpopdict):
    # return get_Zbar(ions, ionpopdict) * get_nntot(ions, ionpopdict)
    nnetot = 0.  # electrons per ion
    for Z, ionstage in ions:
        nnetot += Z * ionpopdict[(Z, ionstage)]

    return nnetot


def get_nne(ions, ionpopdict):
    nne = 0.  # electrons per ion
    for Z, ionstage in ions:
        charge = ionstage - 1
        assert(charge >= 0)
        nne += charge * ionpopdict[(Z, ionstage)]

    return nne


def get_index(en_ev, engrid):
    assert en_ev >= engrid[0]
    assert en_ev < (engrid[-1] + (engrid[1] - engrid[0]))

    for i, en in enumerate(engrid):
        if en < en_ev:
            index = i

    return index


N_e_cache = {}


def calculate_N_e(energy_ev, engrid, ions, ionpopdict, dfcollion, yvec, dftransitions, noexcitation):
    # Kozma & Fransson equation 6.
    # Something related to a number of electrons, needed to calculate the heating fraction in equation 3
    # not valid for energy > E_0
    if energy_ev == 0.:
        return 0.

    if energy_ev in N_e_cache:
        # print("returning cached value for", energy_ev)
        return N_e_cache[energy_ev]
    # else:
    #     print("Cache miss for", energy_ev)

    N_e = 0.

    E_0 = engrid[0]
    deltaen = engrid[1] - engrid[0]

    for Z, ionstage in ions:
        N_e_ion = 0.
        nnion = ionpopdict[(Z, ionstage)]
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)

        for index, shell in dfcollion_thision.iterrows():
            ionpot_ev = shell.ionpot_ev

            enlambda = min(engrid[-1] - energy_ev, energy_ev + ionpot_ev)
            J = get_J(shell.Z, shell.ionstage, ionpot_ev)

            ar_xs_array = at.nonthermal.get_arxs_array_shell(engrid, shell)

            # integral from ionpot to enlambda
            # delta_endash = engrid[1] - engrid[0]

            delta_endash = (enlambda - ionpot_ev) / 1000.
            if delta_endash >= 0:
                endashlist = np.arange(ionpot_ev, enlambda, delta_endash)
                for endash in endashlist:
                    i = get_index(en_ev=energy_ev + endash, engrid=engrid)
                    N_e_ion += (
                        yvec[i] * ar_xs_array[i] *
                        Psecondary(e_p=energy_ev + endash, epsilon=endash, ionpot_ev=ionpot_ev, J=J) * delta_endash)

            # // integral from 2E + I up to E_max
            delta_endash = (engrid[-1] - (2 * energy_ev + ionpot_ev)) / 100.
            if delta_endash >= 0:
                endashlist = np.arange(2 * energy_ev + ionpot_ev, engrid[-1], delta_endash)
                for endash in endashlist:
                    i = get_index(en_ev=endash, engrid=engrid)
                    N_e_ion += (
                        yvec[i] * ar_xs_array[i] *
                        Psecondary(e_p=endash, epsilon=energy_ev + ionpot_ev, ionpot_ev=ionpot_ev, J=J) * delta_endash)
                    # print(endash, energy_ev + ionpot_ev, Psecondary(e_p=endash, epsilon=energy_ev + ionpot_ev, ionpot_ev=ionpot_ev, J=J))

        N_e += nnion * N_e_ion

    if not noexcitation:
        for Z, ion_stage in ions:
            for _, row in dftransitions[(Z, ion_stage)].iterrows():
                nnlevel = row.lower_pop
                # nnlevel = nnion
                epsilon_trans_ev = row.epsilon_trans_ev
                if epsilon_trans_ev >= engrid[0]:
                    i = get_index(en_ev=energy_ev + epsilon_trans_ev, engrid=engrid)
                    xsvec = get_xs_excitation_vector(engrid, row)
                    N_e += nnlevel * epsilon_trans_ev * xsvec[i] * yvec[i]

    # source term not here because it should be zero at the low end anyway
    N_e_cache[energy_ev] = N_e
    return N_e


def calculate_frac_heating(
        engrid, ions, ionpopdict, dfcollion, dftransitions, yvec, nne, nnetot, deposition_density_ev, noexcitation):
    # Kozma & Fransson equation 8

    frac_heating = 0.
    E_0 = engrid[0]

    deltaen = engrid[1] - engrid[0]
    npts = len(engrid)
    for i, en_ev in enumerate(engrid):
        weight = 1 if (i == 0 or i == npts - 1) else 2
        frac_heating += 0.5 * weight * lossfunction(en_ev, nne, nnetot) * yvec[i] * deltaen / deposition_density_ev

    frac_heating += E_0 * yvec[0] * lossfunction(E_0, nne, nnetot) / deposition_density_ev
    # print(f"            frac_heating E_0 * y * l(E_0) part: {E_0 * yvec[0] * lossfunction(E_0, nne) / deposition_density_ev}")

    frac_heating_N_e = 0.
    delta_en = E_0 / 10.
    for en_ev in np.arange(0., E_0, delta_en):
        N_e = calculate_N_e(en_ev, engrid, ions, ionpopdict, dfcollion, yvec, dftransitions, noexcitation=noexcitation)
        frac_heating_N_e += N_e * en_ev * delta_en / deposition_density_ev

    # print(f"            frac_heating N_e part: {frac_heating_N_e}")
    frac_heating += frac_heating_N_e

    return frac_heating


def sfmatrix_add_excitation(engrid, dftransitions_ion, nnion, sfmatrix):
    deltaen = engrid[1] - engrid[0]
    npts = len(engrid)
    for _, row in dftransitions_ion.iterrows():
        nnlevel = row.lower_pop
        epsilon_trans_ev = row.epsilon_trans_ev
        if epsilon_trans_ev >= engrid[0]:
            vec_xs_excitation_nnlevel_deltae = nnlevel * deltaen * get_xs_excitation_vector(engrid, row)
            for i, en in enumerate(engrid):
                stopindex = i + math.ceil(epsilon_trans_ev / deltaen)

                if (stopindex < npts - 1):
                    sfmatrix[i, i: stopindex - i + 1] += vec_xs_excitation_nnlevel_deltae[i: stopindex - i + 1]


def sfmatrix_add_ionization_shell(engrid, nnion, shell, sfmatrix):
    # this code has been optimised and is now an almost unreadable form, but it is the contains the terms
    # related to ionisation cross sections
    deltaen = engrid[1] - engrid[0]
    ionpot_ev = shell.ionpot_ev
    J = get_J(shell.Z, shell.ionstage, ionpot_ev)
    npts = len(engrid)

    ar_xs_array = at.nonthermal.get_arxs_array_shell(engrid, shell)

    if ionpot_ev <= engrid[0]:
        xsstartindex = 0
    else:
        xsstartindex = get_index(en_ev=ionpot_ev, engrid=engrid)

    for i, en in enumerate(engrid):
        # P_sum = 0.
        # if en >= ionpot_ev:
        #     e_s_min = 0.
        #     e_s_max = (en - ionpot_ev) / 2.
        #     delta_e_s = (e_s_max - e_s_min) / 1000.
        #     for e_s in np.arange(e_s_min, e_s_max, delta_e_s):
        #         P_sum += Psecondary(e_p=en, e_s=e_s, ionpot_ev=ionpot_ev, J=J) * delta_e_s
        #
        #     epsilon_upper = e_s_max + ionpot_ev
        #     epsilon_lower = e_s_min + ionpot_ev
        #     P_int = 1. / atan((en - ionpot_ev) / 2. / J) * (atan((epsilon_upper - ionpot_ev) / J) - atan((epsilon_lower - ionpot_ev) / J))
        #
        #     print(f"E_p {en} eV, prob integral e_s from {e_s_min:5.2f} eV to {e_s_max:5.2f}: {P_sum:5.2f} analytical {P_int:5.2f}")

        # // endash ranges from en to SF_EMAX, but skip over the zero-cross section points
        jstart = i if i > xsstartindex else xsstartindex
        if 2 * en + ionpot_ev < engrid[-1] + (engrid[1] - engrid[0]):
            secondintegralstartindex = get_index(2 * en + ionpot_ev, engrid)
        else:
            secondintegralstartindex = npts + 1

        # integral/J of 1/[1 + (epsilon - ionpot_ev) / J] for epsilon = en + ionpot_ev
        for j in range(jstart, npts):
            # j is the matrix column index which corresponds to the piece of the
            # integral at y(E') where E' >= E and E' = envec(j)
            endash = engrid[j]
            prefactor = nnion * ar_xs_array[j] / atan((endash - ionpot_ev) / 2. / J) * deltaen
            assert not np.isnan(prefactor)
            assert not np.isinf(prefactor)
            # assert prefactor >= 0

            # J * atan[(epsilon - ionpot_ev) / J] is the indefinite integral of
            # 1/(1 + (epsilon - ionpot_ev)^2/ J^2) d_epsilon
            # in Kozma & Fransson 1992 equation 4

            # KF 92 limit
            epsilon_upper = (endash + ionpot_ev) / 2
            # Li+2012 limit
            # epsilon_upper = (endash + en) / 2

            int_eps_upper = atan((epsilon_upper - ionpot_ev) / J)

            epsilon_lower = endash - en
            int_eps_lower = atan((epsilon_lower - ionpot_ev) / J)
            # if epsilon_lower > epsilon_upper:
            # #     # print(j, jstart, epsilon_lower, epsilon_upper, int_eps_lower, int_eps_upper)
            #     epsilon_lower, epsilon_upper = epsilon_upper, epsilon_lower
            #     int_eps_lower, int_eps_upper = int_eps_upper, int_eps_lower
            # assert epsilon_lower < epsilon_upper
            # if epsilon_upper > epsilon_lower:
            sfmatrix[i, j] += prefactor * (int_eps_upper - int_eps_lower)

            epsilon_lower = en + ionpot_ev
            epsilon_upper = (endash + ionpot_ev) / 2
            # endash ranges from 2 * en + ionpot_ev to SF_EMAX
            if j >= secondintegralstartindex + 1:
                # int_eps_upper = atan((epsilon_upper - ionpot_ev) / J)
                int_eps_lower = atan((epsilon_lower - ionpot_ev) / J)
                if epsilon_lower > epsilon_upper:
                    print(j, secondintegralstartindex, epsilon_lower, epsilon_upper)
                assert epsilon_lower <= epsilon_upper

                sfmatrix[i, j] -= prefactor * (int_eps_upper - int_eps_lower)


def differentialsfmatrix_add_ionization_shell(engrid, nnion, shell, sfmatrix):
    # this code has been optimised and is now an almost unreadable form, but it is the contains the terms
    # related to ionisation cross sections
    delta_en = engrid[1] - engrid[0]
    ionpot_ev = shell.ionpot_ev
    J = get_J(shell.Z, shell.ionstage, ionpot_ev)
    npts = len(engrid)

    ar_xs_array = at.nonthermal.get_arxs_array_shell(engrid, shell)

    if ionpot_ev <= engrid[0]:
        xsstartindex = 0
    else:
        xsstartindex = get_index(en_ev=ionpot_ev, engrid=engrid)

    oneoveratangrid = 1. / np.arctan((engrid - ionpot_ev) / 2. / J)

    epsilon_lower_a = ionpot_ev
    int_eps_lower_a = atan((epsilon_lower_a - ionpot_ev) / J)
    for i in range(xsstartindex, npts):
        en = engrid[i]

        # integral of xs_ion(e_p=en, epsilon) with epsilon from I to (I + E) / 2

        epsilon_upper = (ionpot_ev + en) / 2.

        if (epsilon_lower_a < epsilon_upper):
            # P_int = 0.
            # eps_npts = 1000
            # delta_eps = (epsilon_upper - epsilon_lower) / eps_npts
            # for j in range(eps_npts):
            #     epsilon = epsilon_lower + j * delta_eps
            #     P_int += Psecondary(e_p=en, epsilon=epsilon, ionpot_ev=ionpot_ev, J=J) * delta_eps
            #
            # J * atan[(epsilon - ionpot_ev) / J] is the indefinite integral of
            # 1/(1 + (epsilon - ionpot_ev)^2/ J^2) d_epsilon

            int_eps_upper = atan((epsilon_upper - ionpot_ev) / J)
            P_int = 1. / atan((en - ionpot_ev) / 2. / J) * (int_eps_upper - int_eps_lower_a)
            # if int_eps_lower == int_eps_upper and epsilon_upper != epsilon_lower:
            # if (abs(P_int2 / P_int - 1.) > 0.2):
            #     print("warning eps low high int low high", epsilon_lower, epsilon_upper, int_eps_lower, int_eps_upper)
            #     print(f'{P_int=:.2e} {P_int2=:.2e} Ratio: {P_int2 / P_int:.2f}')

            sfmatrix[i, i] += nnion * ar_xs_array[i] * P_int

        enlambda = min(engrid[-1] - en, en + ionpot_ev)
        epsilon_lower = ionpot_ev
        epsilon_upper = enlambda
        if (epsilon_lower < epsilon_upper):
            eps_npts = 100
            delta_eps = (epsilon_upper - epsilon_lower) / eps_npts
            prefactor = nnion / J / atan((en - ionpot_ev) / 2. / J) * delta_eps
            for j in range(eps_npts):
                epsilon = epsilon_lower + j * delta_eps
                i_enpluseps = get_index(en + epsilon, engrid=engrid)
                sfmatrix[i, i_enpluseps] -= prefactor * ar_xs_array[i_enpluseps] / (
                    1 + (((epsilon - ionpot_ev) / J) ** 2))

            # j_lower = get_index(en + epsilon_lower, engrid=engrid)
            # j_upper = get_index(en + epsilon_upper, engrid=engrid)
            # if (j_lower < j_upper):
            #     delta_eps = (epsilon_upper - epsilon_lower) / (j_upper - j_lower)
            #     prefactor = nnion / J / atan((en - ionpot_ev) / 2. / J) * delta_eps
            #     print(J, atan((en - ionpot_ev) / 2. / J))
            #     assert not math.isnan(prefactor)
            #     assert not math.isinf(prefactor)
            #     # for j in range(j_lower, j_upper):
            #     #     en_plus_epsilon = engrid[j]
            #     #     epsilon = en_plus_epsilon - en
            #     #     sfmatrix[i, j] -= prefactor * ar_xs_array[j] / (1 + (((epsilon - ionpot_ev) / J) ** 2))
            #     sfmatrix[i, j_lower:j_upper] -= prefactor * ar_xs_array[j_lower:j_upper] / (1 + ((((engrid[j_lower:j_upper] - en) - ionpot_ev) / J) ** 2))

        if (2 * en + ionpot_ev) < engrid[-1]:
            epsilon = en + ionpot_ev
            prefactor = nnion / J / (1 + (((epsilon - ionpot_ev) / J) ** 2)) * delta_en
            i_endash_lower = get_index(2 * en + ionpot_ev, engrid)
            # for j in range(i_endash_lower, npts):
            #     sfmatrix[i, j] -= prefactor * ar_xs_array[j] * oneoveratangrid[j]
            sfmatrix[i, i_endash_lower:] -= prefactor * ar_xs_array[i_endash_lower:] * oneoveratangrid[i_endash_lower:]


def get_d_etaexcitation_by_d_en_vec(engrid, yvec, ions, dftransitions, deposition_density_ev):
    npts = len(engrid)
    part_integrand = np.zeros(npts)

    for Z, ion_stage in ions:
        for _, row in dftransitions[(Z, ion_stage)].iterrows():
            nnlevel = row.lower_pop
            # nnlevel = nnion
            epsilon_trans_ev = row.epsilon_trans_ev
            if epsilon_trans_ev >= engrid[0]:
                xsvec = get_xs_excitation_vector(engrid, row)
                part_integrand += (nnlevel * epsilon_trans_ev * xsvec / deposition_density_ev)

    return yvec * part_integrand


def get_d_etaion_by_d_en_vec(engrid, yvec, ions, ionpopdict, dfcollion, deposition_density_ev):
    npts = len(engrid)
    part_integrand = np.zeros(npts)

    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)
        # print(dfcollion_thision)

        for index, shell in dfcollion_thision.iterrows():
            J = get_J(shell.Z, shell.ionstage, shell.ionpot_ev)
            xsvec = at.nonthermal.get_arxs_array_shell(engrid, shell)

            part_integrand += (nnion * shell.ionpot_ev * xsvec / deposition_density_ev)

    return yvec * part_integrand


def make_plot(
        engrid, yvec, ions, ionpopdict, dfcollion, dftransitions, nne, nnetot,
        sourcevec, deposition_density_ev, outputfilename, noexcitation):

    fs = 13
    fig, axes = plt.subplots(nrows=3, ncols=1, sharex=True,
                             figsize=(6, 8), tight_layout={"pad": 0.3, "w_pad": 0.0, "h_pad": 0.0})

    npts = len(engrid)
    E_0 = engrid[0]

    # E_init_ev = np.dot(engrid, sourcevec) * deltaen
    # d_etasource_by_d_en_vec = engrid * sourcevec / E_init_ev
    # axes[0].plot(engrid[1:], d_etasource_by_d_en_vec[1:], marker="None", lw=1.5, color='blue', label='Source')

    d_etaion_by_d_en_vec = get_d_etaion_by_d_en_vec(engrid, yvec, ions, ionpopdict, dfcollion, deposition_density_ev)

    if not noexcitation:
        d_etaexc_by_d_en_vec = get_d_etaexcitation_by_d_en_vec(engrid, yvec, ions, dftransitions, deposition_density_ev)
    else:
        d_etaexc_by_d_en_vec = np.zeros(npts)

    d_etaheat_by_d_en_vec = [lossfunction(engrid[i], nne, nnetot) * yvec[i] / deposition_density_ev for i in range(len(engrid))]

    axes[-1].plot(engrid, np.log10(yvec), marker="None", lw=1.5, color='black')

    deltaen = engrid[1] - engrid[0]
    etaion_int = np.zeros(npts)
    etaexc_int = np.zeros(npts)
    etaheat_int = np.zeros(npts)
    for i in reversed(range(len(engrid) - 1)):
        etaion_int[i] = etaion_int[i + 1] + d_etaion_by_d_en_vec[i] * deltaen
        etaexc_int[i] = etaexc_int[i + 1] + d_etaexc_by_d_en_vec[i] * deltaen
        etaheat_int[i] = etaheat_int[i + 1] + d_etaheat_by_d_en_vec[i] * deltaen

    etaheat_int[0] += E_0 * yvec[0] * lossfunction(E_0, nne, nnetot) / deposition_density_ev

    etatot_int = etaion_int + etaexc_int + etaheat_int

    # go below E_0
    deltaen = E_0 / 20.
    engrid_low = np.arange(0., E_0, deltaen)
    npts_low = len(engrid_low)
    d_etaheat_by_d_en_low = np.zeros(len(engrid_low))
    etaheat_int_low = np.zeros(len(engrid_low))
    etaion_int_low = np.zeros(len(engrid_low))
    etaexc_int_low = np.zeros(len(engrid_low))
    x = 0
    for i in reversed(range(len(engrid_low))):
        en_ev = engrid_low[i]
        N_e = calculate_N_e(en_ev, engrid, ions, ionpopdict, dfcollion, yvec, dftransitions, noexcitation=noexcitation)
        d_etaheat_by_d_en_low[i] += N_e * en_ev / deposition_density_ev  # + (yvec[0] * lossfunction(E_0, nne, nnetot) / deposition_density_ev)
        etaheat_int_low[i] = (
            (etaheat_int_low[i + 1] if i < len(engrid_low) - 1 else etaheat_int[0]) +
            d_etaheat_by_d_en_low[i] * deltaen)

        etaion_int_low[i] = etaion_int[0]  # cross sections start above E_0
        etaexc_int_low[i] = etaexc_int[0]

    etatot_int_low = etaion_int_low + etaexc_int_low + etaheat_int_low
    engridfull = np.append(engrid_low, engrid)

    axes[0].plot(engridfull, np.append(etaion_int_low, etaion_int), marker="None", lw=1.5,
                 color='C0', label='Ionisation')

    if not noexcitation:
        axes[0].plot(engridfull, np.append(etaexc_int_low, etaexc_int), marker="None", lw=1.5,
                     color='C1', label='Excitation')

    axes[0].plot(engridfull, np.append(etaheat_int_low, etaheat_int), marker="None", lw=1.5,
                 color='C2', label='Heating')

    axes[0].plot(engridfull, np.append(etatot_int_low, etatot_int), marker="None", lw=1.5, color='black', label='Total')

    axes[0].set_ylim(bottom=0)
    axes[0].legend(loc='best', handlelength=2, frameon=False, numpoints=1, prop={'size': 10})
    axes[0].set_ylabel(r'$\eta$ E to Emax', fontsize=fs)

    # axes[1].plot(engrid, d_etaheat_by_d_en_vec / d_etaion_by_d_en_vec, marker="None", lw=1.5, color='C0', label='Heating / Ionisation')
    axes[1].plot(engridfull, np.append(np.zeros(npts_low), d_etaion_by_d_en_vec), marker="None", lw=1.5, color='C0', label='Ionisation')

    if not noexcitation:
        axes[1].plot(engridfull, np.append(np.zeros(npts_low), d_etaexc_by_d_en_vec), marker="None", lw=1.5, color='C1', label='Excitation')

    # axes[1].plot(engridfull, np.append(d_etaheat_by_d_en_low, d_etaheat_by_d_en_vec), marker="None",
    #              lw=1.5, color='C2', label='Heating')
    axes[1].plot(engrid, d_etaheat_by_d_en_vec, marker="None", lw=1.5, color='C2', label='Heating')

    axes[1].set_ylim(bottom=0)
    axes[1].legend(loc='best', handlelength=2, frameon=False, numpoints=1, prop={'size': 10})
    axes[1].set_ylabel(r'd $\eta$ / dE [eV$^{-1}$]', fontsize=fs)

    etatot_int = etaion_int + etaexc_int + etaheat_int

    #    plt.setp(plt.getp(ax, 'xticklabels'), fontsize=fsticklabel)
    #    plt.setp(plt.getp(ax, 'yticklabels'), fontsize=fsticklabel)
    #    for axis in ['top','bottom','left','right']:
    #        ax.spines[axis].set_linewidth(framewidth)
    #    ax.annotate(modellabel, xy=(0.97, 0.95), xycoords='axes fraction', horizontalalignment='right',
    #                verticalalignment='top', fontsize=fs)
    # ax.set_yscale('log')
    axes[-1].set_xlim(0., engrid[-1] * 1.)
    # ax.set_ylim(bottom=5, top=14)
    axes[-1].set_xlabel(r'Electron energy [eV]', fontsize=fs)
    axes[-1].set_ylabel(r'log y(E) [s$^{-1}$ cm$^{-2}$ eV$^{-1}$]', fontsize=fs)
    print(f"Saving '{outputfilename}'")
    fig.savefig(str(outputfilename), format='pdf')
    plt.close()


def solve_spencerfano_differentialform(
        ions, ionpopdict, dfpops, nne, deposition_density_ev, engrid, sourcevec, dfcollion, args,
        adata=None, noexcitation=False):

    deltaen = engrid[1] - engrid[0]
    npts = len(engrid)
    nnetot = get_nnetot(ions, ionpopdict)

    print(f'\nSetting up differential-form Spencer-Fano equation with {npts} energy points'
          f' from {engrid[0]} to {engrid[-1]} eV...')

    E_init_ev = np.dot(engrid, sourcevec) * deltaen
    print(f'    E_init: {E_init_ev:7.2f} eV/s/cm3')

    constvec = np.zeros(npts)
    for i in range(npts):
        constvec[i] += sourcevec[i]

    lossfngrid = np.array([lossfunction(en, nne, nnetot) for en in engrid])

    sfmatrix = np.zeros((npts, npts))
    for i in range(npts):
        en = engrid[i]

        # - d/dE(y[E] * lossfn[E]) = - dy/dE(E) * lossfn(E) - y(E) * dlossfn/dE

        # - dy/dE(E) * lossfn(E) = - (y(E + deltaE) - y(E) ) / deltaen * lossfunction(E)

        # - ( - y(E) * lossfunction)
        sfmatrix[i, i] += lossfngrid[i] / deltaen

        if i + 1 < npts:
            # - y(E + deltaE) * lossfunction
            sfmatrix[i, i + 1] -= lossfngrid[i] / deltaen

        # - y(E) * dlossfn/dE
        sfmatrix[i, i] -= (lossfunction(en + deltaen, nne, nnetot) - lossfngrid[i]) / deltaen

    dftransitions = {}

    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        print(f'  including Z={Z} ion_stage {ionstage} ({at.get_ionstring(Z, ionstage)}) {nnion=:.2e} ionization', end='')
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)
        # print(dfcollion_thision)

        for index, shell in dfcollion_thision.iterrows():
            assert shell.ionpot_ev >= engrid[0]
            differentialsfmatrix_add_ionization_shell(engrid, nnion, shell, sfmatrix)

        assert noexcitation

        print()

    print()
    lu_and_piv = linalg.lu_factor(sfmatrix, overwrite_a=False)
    yvec_reference = linalg.lu_solve(lu_and_piv, constvec, trans=0)
    yvec = yvec_reference * deposition_density_ev / E_init_ev

    return yvec, dftransitions


def solve_spencerfano(
        ions, ionpopdict, dfpops, nne, deposition_density_ev, engrid, sourcevec, dfcollion, args,
        adata=None, noexcitation=False):

    deltaen = engrid[1] - engrid[0]
    npts = len(engrid)
    nnetot = get_nnetot(ions, ionpopdict)

    print(f'\nSetting up Spencer-Fano equation with {npts} energy points from {engrid[0]} to {engrid[-1]} eV...')

    E_init_ev = np.dot(engrid, sourcevec) * deltaen
    print(f'    E_init: {E_init_ev:7.2f} eV/s/cm3')

    constvec = np.zeros(npts)
    for i in range(npts):
        for j in range(i, npts):
            constvec[i] += sourcevec[j] * deltaen

    sfmatrix = np.zeros((npts, npts))
    for i in range(npts):
        en = engrid[i]
        sfmatrix[i, i] += lossfunction(en, nne, nnetot)
        # EV = 1.6021772e-12  # in erg
        # print(f"electron loss rate nne={nne:.3e} and {i:d} {en:.2e} eV is {lossfunction(en, nne):.2e} or '
        #       f'{lossfunction_ergs(en * EV, nne) / EV:.2e}")

    dftransitions = {}

    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        print(f'  including Z={Z} ion_stage {ionstage} ({at.get_ionstring(Z, ionstage)}) nnion={nnion:.1e}) ionization', end='')
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)
        # print(dfcollion_thision)

        for index, shell in dfcollion_thision.iterrows():
            assert shell.ionpot_ev >= engrid[0]
            sfmatrix_add_ionization_shell(engrid, nnion, shell, sfmatrix)

        if not noexcitation:
            dfpops_thision = dfpops.query('Z==@Z & ion_stage==@ionstage')
            popdict = {x.level: x['n_NLTE'] for _, x in dfpops_thision.iterrows()}

            print(' and excitation ', end='')
            ion = adata.query('Z == @Z and ion_stage == @ionstage').iloc[0]
            groundlevelnoj = ion.levels.iloc[0].levelname.split('[')[0]
            topgmlevel = ion.levels[ion.levels.levelname.str.startswith(groundlevelnoj)].index.max()
            # topgmlevel = float('inf')
            topgmlevel = 4
            dftransitions[(Z, ionstage)] = ion.transitions.query('lower <= @topgmlevel', inplace=False).copy()

            print(f'with {len(dftransitions[(Z, ionstage)])} transitions from lower <= {topgmlevel}', end='')

            if not dftransitions[(Z, ionstage)].empty:
                dftransitions[(Z, ionstage)].query('collstr >= 0 or forbidden == False', inplace=True)
                dftransitions[(Z, ionstage)].eval(
                    'epsilon_trans_ev = '
                    '@ion.levels.loc[upper].energy_ev.values - @ion.levels.loc[lower].energy_ev.values',
                    inplace=True)
                dftransitions[(Z, ionstage)].query('epsilon_trans_ev >= @engrid[0]', inplace=True)
                dftransitions[(Z, ionstage)].eval('lower_g = @ion.levels.loc[lower].g.values', inplace=True)
                dftransitions[(Z, ionstage)].eval('upper_g = @ion.levels.loc[upper].g.values', inplace=True)
                dftransitions[(Z, ionstage)]['lower_pop'] = dftransitions[(Z, ionstage)].apply(
                    lambda x: popdict.get(x.lower, 0.), axis=1)

                sfmatrix_add_excitation(engrid, dftransitions[(Z, ionstage)], nnion, sfmatrix)

        print()

    print()
    lu_and_piv = linalg.lu_factor(sfmatrix, overwrite_a=False)
    yvec_reference = linalg.lu_solve(lu_and_piv, constvec, trans=0)
    yvec = yvec_reference * deposition_density_ev / E_init_ev

    return yvec, dftransitions


def get_nne_nt(engrid, yvec):
    # oneovervelocity = np.sqrt(9.10938e-31 / 2 / engrid / 1.60218e-19) / 100  # in s/cm
    # enovervelocity = engrid * oneovervelocity
    # en_tot = np.dot(yvec, enovervelocity) * (engrid[1] - engrid[0])
    nne_nt = 0.
    deltaen = (engrid[1] - engrid[0])
    for i, en in enumerate(engrid):
        # oneovervelocity = np.sqrt(9.10938e-31 / 2 / en / 1.60218e-19) / 100.
        velocity = np.sqrt(2 * en * 1.60218e-19 / 9.10938e-31) * 100.  # cm/s
        nne_nt += yvec[i] / velocity * deltaen

    return nne_nt


def analyse_ntspectrum(
        engrid, yvec, ions, ionpopdict, nntot, nne, deposition_density_ev, dfcollion, dftransitions,
        noexcitation, modelpath):

    deltaen = engrid[1] - engrid[0]

    frac_ionization = 0.
    frac_excitation = 0.
    frac_ionization_ion = {}
    frac_excitation_ion = {}
    gamma_nt = {}

    electron_binding = read_binding_energies(modelpath)

    Zbar = get_Zbar(ions, ionpopdict)
    nnetot = get_nnetot(ions, ionpopdict)

    Aconst = 1.33e-14 * EV * EV

    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        X_ion = nnion / nntot
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)
        # if dfcollion.empty:
        #     continue
        ionpot_valence = dfcollion_thision.ionpot_ev.min()

        print(f'====> Z={Z:2d} {at.get_ionstring(Z, ionstage)} (valence potential {ionpot_valence:.1f} eV)')

        print(f'               nnion: {nnion:.2e} /cm3')
        print(f'         nnion/nntot: {X_ion:.5f}')

        frac_ionization_ion[(Z, ionstage)] = 0.
        # integralgamma = 0.
        eta_over_ionpot_sum = 0.
        for index, shell in dfcollion_thision.iterrows():
            xsstartindex = get_index(en_ev=shell.ionpot_ev, engrid=engrid)
            ar_xs_array = at.nonthermal.get_arxs_array_shell(engrid, shell)

            frac_ionization_shell = (
                nnion * shell.ionpot_ev * np.dot(yvec, ar_xs_array) * deltaen / deposition_density_ev)
            print(f'frac_ionization_shell(n {int(shell.n):d} l {int(shell.l):d}): '
                  f'{frac_ionization_shell:.4f} (ionpot {shell.ionpot_ev:.2f} eV)')

            # integralgamma += np.dot(yvec, ar_xs_array) * deltaen * shell.ionpot_ev / ionpot_valence

            if frac_ionization_shell > 1:
                frac_ionization_shell = 0.
                print(f'Ignoring frac_ionization_shell of {frac_ionization_shell}.')
                # for k in range(10):
                #     print(nnion * shell.ionpot_ev * yvec_reference[k] * ar_xs_array[k] * deltaen / E_init_ev)

            frac_ionization_ion[(Z, ionstage)] += frac_ionization_shell
            eta_over_ionpot_sum += frac_ionization_shell / shell.ionpot_ev
            print(f'  cross section at {engrid[xsstartindex + 1]:.2e} eV and {engrid[-1]:.2e} eV {ar_xs_array[xsstartindex + 1]:.2e} and {ar_xs_array[-1]:.2e}')

        frac_ionization += frac_ionization_ion[(Z, ionstage)]

        eff_ionpot_2 = X_ion / eta_over_ionpot_sum if eta_over_ionpot_sum else float('inf')

        try:
            eff_ionpot = ionpot_valence * X_ion / frac_ionization_ion[(Z, ionstage)]
        except ZeroDivisionError:
            eff_ionpot = float('inf')

        print(f'     frac_ionization: {frac_ionization_ion[(Z, ionstage)]:.4f}')
        if not noexcitation:
            frac_excitation_ion[(Z, ionstage)] = calculate_nt_frac_excitation(
                engrid, dftransitions[(Z, ionstage)], yvec, deposition_density_ev)
            if frac_excitation_ion[(Z, ionstage)] > 1:
                frac_excitation_ion[(Z, ionstage)] = 0.
                print('Ignoring frac_excitation_ion of {frac_excitation_ion[(Z, ionstage)]}.')
            frac_excitation += frac_excitation_ion[(Z, ionstage)]
            print(f'     frac_excitation: {frac_excitation_ion[(Z, ionstage)]:.4f}')
        else:
            frac_excitation_ion[(Z, ionstage)] = 0.

        print(f' eff_ionpot_shellpot: {eff_ionpot_2:.2f} eV')
        print(f'  eff_ionpot_valence: {eff_ionpot:.2f} eV')
        gamma_nt[(Z, ionstage)] = deposition_density_ev / nntot / eff_ionpot
        print(f'  Spencer-Fano Gamma: {gamma_nt[(Z, ionstage)]:.2e}')
        # print(f'Alternative Gamma: {integralgamma:.2e}')

        binding = get_mean_binding_energy(Z, ionstage, electron_binding, ionpot_ev=ionpot_valence)  # binding in erg
        oneoverW = Aconst * binding / Zbar / (2 * 3.14159 * pow(QE, 4))  # per erg
        deposition_density_erg = deposition_density_ev * EV

        # to get the non-thermal ionization rate we need to divide the energy deposited
        # per unit volume per unit time in the grid cell (sum of terms above)
        # by the total ion number density and the "work per ion pair"
        print(f"       work function: {1. / oneoverW / EV:.2f} eV")
        print(f"   work fn ratecoeff: {deposition_density_erg / nntot * oneoverW:.2e}")
        # Axelrod 1980 Eq 3.225 with E0 = E = E_max
        # xs = lossfunction(engrid[-1], nne, nnetot) * EV * oneoverW / nntot
        # print(f"         WFApprox xs: {xs:.2e} cm^2")

        print()

    # nne_nt = get_nne_nt(engrid, yvec)
    # print(f'               nne_nt: {nne_nt:.2e} /s/cm3')

    print(f'  frac_excitation_tot: {frac_excitation:.4f}')
    print(f'  frac_ionization_tot: {frac_ionization:.4f}')

    frac_heating = calculate_frac_heating(
        engrid, ions, ionpopdict, dfcollion, dftransitions, yvec, nne, nnetot, deposition_density_ev,
        noexcitation=noexcitation)

    print(f'         frac_heating: {frac_heating:.4f}')
    print(f'             frac_sum: {frac_excitation + frac_ionization + frac_heating:.4f}')

    return frac_excitation, frac_ionization, frac_excitation_ion, frac_ionization_ion, gamma_nt


def get_Latom_axelrod(Zboundbar, en_ev):
    # Axelrod 1980 Eq 3.21
    # Latom is 1/N * dE/dX where E is in erg

    en_erg = en_ev * EV

    # relativistic
    gamma = en_erg / (ME * CLIGHT ** 2) + 1
    beta = math.sqrt(1. - 1. / (gamma ** 2))
    vel = beta * CLIGHT  # in cm/s

    # classical
    # vel = math.sqrt(2. * en_erg / ME)
    # beta = vel / CLIGHT

    # I = ionpot_ev * EV
    I = 280 * EV  # assumed in Axelrod thesis

    if 2 * ME * vel ** 2 < I:
        return 0.

    # if beta > 1.:
    #     print(vel, beta)
    #     beta = 0.9999

    return 4 * math.pi * QE ** 4 / (ME * vel ** 2) * Zboundbar * (
        math.log(2 * ME * vel ** 2 / I) + math.log(1. / (1. - beta ** 2)) - beta ** 2)


def get_Lelec_axelrod(en_ev, nne, nnetot, nntot):
    # Axelrod Eq 3.36 (classical low energy limit)
    # return 1.95e-13 * math.log(3.2e4 * en_ev) / en_ev

    # Axelrod 1980 Eq 3.24

    HBAR = H / 2. / math.pi
    en_erg = en_ev * EV
    gamma = en_erg / (ME * CLIGHT ** 2) + 1
    beta = math.sqrt(1. - 1. / (gamma ** 2))
    vel = beta * CLIGHT  # in cm/s
    omegap = 5.64e4 * math.sqrt(nne)  # in per second
    return 4 * math.pi * QE ** 4 / (ME * vel ** 2) * nne / nntot * (
        math.log(2 * ME * vel ** 2 / (HBAR * omegap)) + 0.5 * math.log(1. / (1. - beta ** 2)) - 0.5 * beta ** 2)


def lossfunction_axelrod(en_ev, nne, nnetot):
    # dE / dX
    return get_Lelec_axelrod(en_ev, nne, nnetot, 1)


def get_fij_ln_en_ionisation(emax_ev, J, shell):
    npts = 1000
    e_p_lower = shell.ionpot_ev
    e_p_upper = emax_ev
    delta_e_p = (e_p_upper - e_p_lower) / npts
    sum = 0.
    for i in range(npts):
        e_p = e_p_lower + i * delta_e_p
        print(i, e_p)

        sigma = at.nonthermal.ar_xs(e_p, shell.ionpot_ev, shell.A, shell.B, shell.C, shell.D)
        eps_avg = get_epsilon_avg(e_p, J, shell.ionpot_ev)
        if eps_avg > 0:
            sum += ME * CLIGHT / math.pi / (QE ** 2) / H * sigma * math.log(eps_avg) * delta_e_p

    return sum


def e_s_test(ax, ionpot_ev, J, arr_en_ev, shellstr, color):
    e_p = arr_en_ev[-1]
    npts = 100000
    # ionpot_ev = 280

    J = ionpot_ev * 0.6
    # epsilon_lower = ionpot_ev
    # epsilon_upper = (ionpot_ev + e_p) / 2.
    # delta_eps = (epsilon_upper - epsilon_lower) / npts
    # print(f'{delta_eps=}')
    # prob_sum = 0.
    # eps_avg = 0.
    # prob = np.zeros(npts)
    # epsilon = np.zeros(npts)
    # for i in range(npts):
    #     epsilon[i] = epsilon_lower + i * delta_eps
    #     prob[i] = Psecondary(e_p, ionpot_ev, J, epsilon=epsilon[i]) * delta_eps
    #     prob_sum += prob[i]
    #     eps_avg = epsilon[i] * prob[i]
    # print(f'{prob_sum=:.1e}')
    # print(f'{eps_avg=:.1e}')
    # # ax.plot(epsilon, prob, label=shellstr + ' dP / d_epsilon')

    e_s_lower = 0
    e_s_upper = (e_p - ionpot_ev) / 2.
    delta_e_s = (e_s_upper - e_s_lower) / npts
    prob_sum = 0.
    e_s_avg = 0.
    prob = np.zeros(npts)
    e_s = np.zeros(npts)
    prob_e_s_can_ionise = 0.
    prob_e_s_cannot_ionise = 0.
    for i in range(npts):
        e_s[i] = e_s_lower + i * delta_e_s
        prob[i] = Psecondary(e_p, ionpot_ev, J, e_s=e_s[i]) * delta_e_s
        prob_sum += prob[i]
        e_s_avg = e_s[i] * prob[i]
        if e_s[i] >= ionpot_ev:
            prob_e_s_can_ionise += prob[i]
        else:
            prob_e_s_cannot_ionise += prob[i]

    print(f'{prob_sum=:.2f}')
    print(f'{e_s_avg=:.1e}')
    print(f'{prob_e_s_can_ionise=:.2f}')
    print(f'{prob_e_s_cannot_ionise=:.2f}')
    # ax.plot(e_s, prob, label=shellstr + ' dP / d_e_s', color=color)
    # ax.vlines(ionpot_ev, ymin=0., ymax=max(prob), color=color)


def get_epsilon_avg(e_p, J, ionpot_ev):
    # average energy loss of the primary electron per ionisation in eV
    npts = 1000000

    epsilon_lower = ionpot_ev
    epsilon_upper = (ionpot_ev + e_p) / 2.
    if epsilon_upper <= epsilon_lower:
        return 0.
    delta_eps = (epsilon_upper - epsilon_lower) / npts
    # print(f'{delta_eps=}')
    # print(f'{epsilon_lower} {epsilon_upper} {delta_eps} {e_p}')
    prob_sum = 0.
    prob_e_s_can_ionise = 0.
    prob_e_s_cannot_ionise = 0.
    eps_avg = 0.
    for i in range(npts):
        epsilon = epsilon_lower + i * delta_eps
        prob = Psecondary(e_p, ionpot_ev, J, epsilon=epsilon) * delta_eps
        prob_sum += prob
        eps_avg += epsilon * prob
        e_s = epsilon - ionpot_ev
        if e_s >= ionpot_ev:
            prob_e_s_can_ionise += prob
        else:
            prob_e_s_cannot_ionise += prob
    # print(f'{prob_sum=:.3f}')
    assert(abs(prob_sum - 1.) < 0.30)
    print(f'{e_p=:.1e}')
    print(f'{eps_avg=:.1e}')
    print(f'{prob_e_s_can_ionise=:.2f}')
    print(f'{prob_e_s_cannot_ionise=:.2f}')
    return eps_avg


def calculate_Latom_ionisation(ions, ionpopdict, adata, dfcollion, electron_binding,
                               nntot, nnetot, nne, en_ev, Zboundbar, Zbar):
    L_atom_sum = 0.
    for Z, ionstage in ions:
        nnion = ionpopdict[(Z, ionstage)]
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)
        ionpot_valence_ev = dfcollion_thision.ionpot_ev.min()
        # ln_I_sum = 0.
        # ion = adata.query('Z == @Z and ion_stage == @ionstage').iloc[0]
        # dftransitions_ion = ion.transitions
        # # dftransitions_ion.query('collstr >= 0 or forbidden == False', inplace=True)
        # dftransitions_ion.query('A >= 0', inplace=True)
        # dftransitions_ion.eval(
        #     'epsilon_trans_ev = '
        #     '@ion.levels.loc[upper].energy_ev.values - @ion.levels.loc[lower].energy_ev.values', inplace=True)
        # dftransitions_ion.eval(
        #     'g_ratio = '
        #     '@ion.levels.loc[upper].g.values / @ion.levels.loc[lower].g.values', inplace=True)
        # h_ev_s = 4.1357e-15  # eV seconds
        # for _, row in dftransitions_ion.iterrows():
        #     if row.A > 0:
        #         # nnlevel = row.lower_pop
        #         epsilon_trans_ev = row.epsilon_trans_ev
        #         nu_trans = epsilon_trans_ev / h_ev_s
        #         g_ratio = row.g_ratio
        #         A_ul = row.A
        #         f_ul = g_ratio * ME * pow(CLIGHT, 3) / (8 * pow(QE * nu_trans * PI, 2)) * A_ul
        #         lnI_contrib = f_ul * math.log(epsilon_trans_ev)
        #         ln_I_sum += lnI_contrib
        #         # vec_xs_excitation_nnlevel_deltae = nnlevel * deltaen * get_xs_excitation_vector(engrid, row)
        #
        # print(f'ln I: {ln_I_sum:.2e} (before ionisiation)')
        # print(f'I: {math.exp(ln_I_sum):.2e} (before ionisiation)')

        ionpot_valence_ev = dfcollion_thision.ionpot_ev.min()

        # for _, shell in dfcollion_thision.iterrows():
        #     J = get_J(Z, ionstage, shell.ionpot_ev)
        #
        #     eps_avg = get_epsilon_avg(en_ev, J, shell.ionpot_ev)
        #     # eps_avg = shell.ionpot_ev
        #     if eps_avg > 0:
        #         # sigma = get_lotz_xs_ionisation(Z, ionstage, electron_binding, ionpot_ev=ionpot_valence_ev, en_ev=en_ev)
        #         sigma = at.nonthermal.ar_xs(en_ev, shell.ionpot_ev, shell.A, shell.B, shell.C, shell.D)
        #         # L_atom_sum += ME * CLIGHT / math.pi / (QE ** 2) * sigma * eps_avg * nnion / nntot
        #         L_atom_sum += sigma * eps_avg * nnion / nntot

        J = get_J(Z, ionstage, ionpot_valence_ev)
        eps_avg = get_epsilon_avg(en_ev, J, ionpot_valence_ev)
        # test case: secondary electron has no energy
        # eps_avg = ionpot_valence_ev
        if eps_avg > 0:
            sigma = get_lotz_xs_ionisation(Z, ionstage, electron_binding, ionpot_ev=ionpot_valence_ev, en_ev=en_ev)
            # sigma = at.nonthermal.ar_xs(en_ev, shell.ionpot_ev, shell.A, shell.B, shell.C, shell.D)
            # L_atom_sum += ME * CLIGHT / math.pi / (QE ** 2) * sigma * eps_avg * nnion / nntot
            # nnebound = nnetot - nne
            # 1e-4 = EV * 2 * 3.14 * 1e7
            L_atom_sum += sigma * eps_avg * EV * nnion / nntot

        # print(f'ln I: {ln_I_sum:.2e} (after ionsiation)')
        # print(f'I: {math.exp(ln_I_sum):.2e} (after ionisation)')
    return L_atom_sum


def workfunction_tests(modelpath, args):
    electron_binding = read_binding_energies()
    dfcollion = at.nonthermal.read_colliondata()

    fig, axes = plt.subplots(nrows=1, ncols=1, sharex=True,
                             figsize=(6*.7, 5*.7), tight_layout={"pad": 0.3, "w_pad": 0.0, "h_pad": 0.0})
    axes = [axes]

    ionpopdict = {
        # (16, 2): 3e5,
        (26, 2): 1e5,
    }

    ions = []
    for key in ionpopdict.keys():
        # keep only the ion populations, not element or total populations
        if isinstance(key, tuple) and len(key) == 2:
            ions.append(key)

    ions.sort()

    nntot = get_nntot(ions=ions, ionpopdict=ionpopdict)
    nnetot = get_nnetot(ions=ions, ionpopdict=ionpopdict)  # total electrons: free and bound included
    nne = get_nne(ions=ions, ionpopdict=ionpopdict)
    nnebound = nnetot - nne
    # x_e = nne / nntot
    Zbar = get_Zbar(ions=ions, ionpopdict=ionpopdict)  # average atomic number
    Zboundbar = get_Zboundbar(ions=ions, ionpopdict=ionpopdict)  # average bound electrons per nucleus
    print(f'{nntot=:.1e}')
    print(f'{nnetot=:.1e}')
    print(f'{nne=:.1e}')
    print(f'{nnebound=:.1e}')
    print(f'{Zbar=:.1e}')
    print(f'{Zboundbar=:.1e}')

    hbar_ev_s = 6.58211951e-16  # in eV seconds
    omegap = 5.6e4 * math.sqrt(nne)  # in per second
    print(f'hbar * omegap = {hbar_ev_s * omegap:.2e} eV')

    en_min_ev = args.emin
    en_max_ev = args.emax
    print(f'{en_min_ev=:.1f} eV')
    print(f'{en_max_ev=:.1f} eV')
    # arr_en_ev = np.linspace(en_min_ev, en_max_ev, 2000)
    # delta_en_ev = arr_en_ev[1] - arr_en_ev[0]
    arr_en_ev = np.logspace(start=math.log10(en_min_ev), stop=math.log10(en_max_ev), base=10, num=args.npts, endpoint=True)
    global Psecondary_e_s_max
    Psecondary_e_s_max = arr_en_ev[2]
    print(f'{Psecondary_e_s_max=}')

    delta_en_ev = arr_en_ev[1:] - arr_en_ev[:-1]
    arr_en_ev = arr_en_ev[:-1]  # remove the endpoint, now that we've used it to calculate detla_en_ev

    # Axelrod 1980 Eq 3.24
    Lelec_axelrod_nne = np.array([get_Lelec_axelrod(en_ev=en_ev, nne=nne, nnetot=nnetot, nntot=nntot) for en_ev in arr_en_ev])
    Lelec_axelrod_nnetot = np.array([get_Lelec_axelrod(en_ev=en_ev, nne=nnetot, nnetot=nnetot, nntot=nntot) for en_ev in arr_en_ev])

    Lelec_kf92_nne = np.array([lossfunction(en_ev, nne, nnetot, use_nnetot=False) * EV / nntot for en_ev in arr_en_ev])

    Lelec_kf92_nnetot = np.array([lossfunction(en_ev, nne, nnetot, use_nnetot=True) * EV / nntot for en_ev in arr_en_ev])

    Lelec_kf92_nnebound = np.array([lossfunction(en_ev, nnebound, nnetot, use_nnetot=False) * EV / nntot for en_ev in arr_en_ev])

    # print(f'{Lelec_axelrod[-1]=}')
    # print(f'{Lelec_kf92[-1]=}')

    Latom_axelrod = np.array([get_Latom_axelrod(en_ev=en_ev, Zboundbar=Zboundbar) for en_ev in arr_en_ev])

    # adata = at.get_levels(modelpath, get_transitions=False, ionlist=tuple(ions))
    adata = None
    arr_Latom_summed = np.zeros_like(arr_en_ev)
    for i, en_ev in enumerate(arr_en_ev):
        arr_Latom_summed[i] = calculate_Latom_ionisation(
            ions, ionpopdict, adata, dfcollion, electron_binding,
            nntot=nntot, nnetot=nnetot, nne=nne, en_ev=en_ev, Zboundbar=Zboundbar, Zbar=Zbar)

        print(f'{en_ev:.2f} eV L_atom_summed: {arr_Latom_summed[i]:.3e} (ionisation only) '
              f'Latom_axelrod: {Latom_axelrod[i]:.3e} ratio(sum/axelrod): {arr_Latom_summed[i] / Latom_axelrod[i]:.2e} ratio(axelrod/sum) {Latom_axelrod[i] / arr_Latom_summed[i]:.2e}')

    for Z, ionstage in ions:
        # ionstr = at.get_ionstring(Z, ionstage, spectral=True, nospace=False)
        dfcollion_thision = dfcollion.query('Z == @Z and ionstage == @ionstage', inplace=False)

        # dfcollion_thision.query('n == 3 and l == 2', inplace=True)
        # dfcollion_thision.eval('A = 0', inplace=True)
        # dfcollion_thision.eval('B = 0', inplace=True)
        # # dfcollion_thision.eval('C = 0', inplace=True)
        # dfcollion_thision.eval('D = 0', inplace=True)

        # shellindex = 0
        # for _, shell in dfcollion_thision.iterrows():
        #     shellstr = f'\nn {shell.n} l {shell.l} ionpot {shell.ionpot_ev} eV'
        #     print(shellstr)
        #     e_p = arr_en_ev[-1]
        #     J = get_J(Z, ionstage, shell.ionpot_ev)
        #     e_s_test(axes[-1], shell.ionpot_ev, J, arr_en_ev, shellstr, color=f'C{shellindex}')
        #     shellindex += 1

        ionpot_valence_ev = dfcollion_thision.ionpot_ev.min()
        print(f'\n===> ion {Z=} {ionstage=} {ionpot_valence_ev=}')
        # print(dfcollion_thision)

        Aconst = 1.33e-14 * EV * EV
        binding = get_mean_binding_energy(Z, ionstage, electron_binding, ionpot_ev=ionpot_valence_ev)  # binding in erg
        oneoverW_limit_sim = Aconst * binding / Zbar / (2 * 3.14159 * pow(QE, 4))  # per erg
        workfn_limit_ev_sim = 1. / oneoverW_limit_sim / EV
        print(f'\n workfn_limit_ev_sim {workfn_limit_ev_sim:.2f} eV')
        print(f'   eta_ion  {ionpot_valence_ev / workfn_limit_ev_sim:.3f}')
        print(f'   eta_heat {1 - ionpot_valence_ev / workfn_limit_ev_sim:.3f}')
        arr_workfn_limit_sim = np.array([workfn_limit_ev_sim for x in arr_en_ev])

        arr_xs_ar92 = at.nonthermal.get_arxs_array_ion(arr_en_ev, dfcollion_thision, Z, ionstage)
        Latom_ionisation_ar92 = arr_xs_ar92 * (ionpot_valence_ev * EV)

        arr_xs_lotz = np.array([
            get_lotz_xs_ionisation(Z, ionstage, electron_binding, ionpot_ev=ionpot_valence_ev, en_ev=en_ev)
            for en_ev in arr_en_ev])

        axes[-1].plot(arr_en_ev, Latom_axelrod, label='Latom_axelrod')

        # Axelrod 1980 Eq 3.20, (Latom part).
        # This assumes that the every bound electron cross section is included!
        Latom_ionisation_lotz = arr_xs_lotz * (ionpot_valence_ev * EV)

        L_over_sigma = (Lelec_axelrod_nne[-1] + Latom_axelrod[-1]) / arr_xs_lotz[-1]
        workfn_limit_axelrod = L_over_sigma / EV
        print(f'\n workfn_limit_axelrod: {workfn_limit_axelrod:.2f} eV')
        print(f'   eta_ion  {ionpot_valence_ev / workfn_limit_axelrod:.3f}')
        print(f'   eta_heat {1 - ionpot_valence_ev / workfn_limit_axelrod:.3f}')
        arr_workfn_limit_axelrod = np.array([workfn_limit_axelrod for x in arr_en_ev])

        # Approximation to Axelrod 1980 Eq 3.20 (left Latom part) where the transition energy
        # of every ionisation is just the valence potential
        # Latom_ionisation = arr_xs * (ionpot_valence_ev * EV)
        # print(Latom[-1], Latom2[-1], Latom3[-1])

        # arr_xs_latom = Latom1 / (ionpot_valence_ev * EV)

        # axes[-1].plot(arr_en_ev, arr_xs_lotz, label=r'$\sigma_{Lotz}$')
        # axes[-1].plot(arr_en_ev, arr_xs_ar92, label=r'$\sigma_{AR92}$')
        # axes[-1].plot(arr_en_ev, arr_xs_latom, label=r'$\sigma$=$L_{atom}/I$', linestyle='dashed')

        axes[-1].plot(arr_en_ev, arr_Latom_summed, label=r'Latom_summed')

        # Lelec = Lelec_kf92_nne
        # Lelec = Lelec_kf92_nnetot
        # Lelec = Lelec_kf92_nnebound
        Lelec = Lelec_axelrod_nne
        # Lelec = Lelec_axelrod_nnetot

        Latom = Latom_axelrod
        # Latom = Latom_ionisation_lotz
        # Latom = Latom_ionisation_ar92
        # axes[-1].plot(arr_en_ev, Lelec_kf92_nne / Latom_axelrod, label='Lelec_kf92 / Latom_axelrod')
        # axes[-1].plot(arr_en_ev, Lelec_axelrod / Latom_axelrod, label='Lelec_axelrod / Latom_axelrod')

        # arr_Lelec_over_Latom = [x_e / (Zbar - x_e) * (math.log(4 * en_ev / (hbar_ev_s * omegap)) / math.log(4 * en_ev / 300)) for en_ev in arr_en_ev]
        # axes[-1].plot(arr_en_ev, arr_Lelec_over_Latom, label='Lelec_axelrod / Latom_axelrod analytic')

        # axes[-1].plot(arr_en_ev, Lelec_axelrod / Lelec_kf92_nne, label='lossfunction_axelrod / lossfunction_kf92')

        # Latom = Latom_axelrod - Latom_ionisation_lotz + Latom_ionisation_ar92

        # arr_xs = arr_xs_lotz
        arr_xs = arr_xs_ar92
        # axes[-1].plot(arr_en_ev, arr_xs_ar92 * 1e18, label=ionstr + ' Arnaud & Rotherflug 1992', linewidth=2)
        # axes[-1].plot(arr_en_ev, arr_xs_lotz * 1e18, label=ionstr + ' Lotz (1967)', linewidth=2)

        L = Lelec + Latom

        with np.errstate(divide='ignore'):
            workfn_limit = L / EV / arr_xs

        print(f'\n workfn_limit at Emax: {workfn_limit[-1]:.2f} eV')
        print(f'   eta_ion  {ionpot_valence_ev / workfn_limit[-1]:.3f}')
        print(f'   eta_heat {1 - ionpot_valence_ev / workfn_limit[-1]:.3f}')

        arr_workfn_integrated = np.zeros_like(arr_en_ev)
        integrand = arr_xs / (L / EV)
        # arr_workfn_integrated[i] is the en_ev / (integral xs / L dE from EMIN to E[i])
        with np.errstate(divide='ignore'):
            arr_workfn_integrated = [arr_en_ev[i] / (sum((integrand * delta_en_ev)[:i])) for i in range(len(arr_en_ev))]

        print(f'\n workfn_integral_Emin_Emax: {arr_workfn_integrated[-1]:.2f} eV')
        print(f'   eta_ion  {ionpot_valence_ev / arr_workfn_integrated[-1]:.3f}')
        print(f'   eta_heat {1 - ionpot_valence_ev / arr_workfn_integrated[-1]:.3f}')

        # axes[-1].plot(arr_en_ev, arr_workfn_limit_axelrod, label='workfn limit E->inf (Axelrod)')
        # axes[-1].plot(arr_en_ev, arr_workfn_limit_sim, label='workfn limit E->inf (Sim)')
        # axes[-1].plot(arr_en_ev, workfn_limit, label='workfn integrated near Emax', color='C2')
        # axes[-1].plot(arr_en_ev, arr_workfn_integrated, label='workfn integrated Emin to Emax', color='C3')

        # arr = [lossfunction(en_ev, nne, nnetot) / arr_xs_ar92[i] for i, en_ev in enumerate(arr_en_ev)]
        # arr = [Latom_axelrod[i] / arr_xs_ar92[i] for i, en_ev in enumerate(arr_en_ev)]

        # print(arr)
        # print(min(arr), max(arr))
        # axes[-1].plot(arr_en_ev, arr, label='lossfunc / sigma', color='C3')

    axes[-1].set_xlabel(r'Energy [eV]')
    axes[-1].set_xscale('log')
    # axes[-1].set_xlim(0., 500)
    # axes[-1].set_ylim(0., 2000)
    # axes[-1].set_ylim(bottom=1e-3, top=1e-2)
    # axes[-1].set_ylim(bottom=min(arr) / 2., top=max(arr) * 2.)
    # axes[-1].set_ylabel(r'cross section [cm$^{2}$]')
    # axes[-1].set_ylabel(r'Cross Section $[10^{-18}$ cm$^{2}]$')
    # axes[-1].set_ylabel(r'Ionisation fraction of deposited energy')
    axes[-1].set_yscale('log')
    # axes[-1].set_ylabel(r'log y(E) [s$^{-1}$ cm$^{-2}$ eV$^{-1}$]', fontsize=fs)
    # axes[-1].yaxis.set_minor_locator(ticker.MultipleLocator(base=5))
    axes[-1].legend(frameon=False, loc='upper right')
    outputfilename = 'plot.pdf'
    print(f"Saving '{outputfilename}'")
    fig.savefig(str(outputfilename), format='pdf')
    plt.close()


def addargs(parser):
    parser.add_argument('-modelpath', default='.',
                        help='Path to ARTIS folder')

    parser.add_argument('-timedays', '-time', '-t',
                        help='Time in days to plot')

    parser.add_argument('-timestep', '-ts', type=int,
                        help='Timestep number to plot')

    parser.add_argument('-modelgridindex', '-cell', type=int, default=0,
                        help='Modelgridindex to plot')

    parser.add_argument('-velocity', '-v', type=float, default=-1,
                        help='Specify cell by velocity')

    parser.add_argument('-npts', type=int, default=4096,
                        help='Number of points in the energy grid')

    parser.add_argument('-emin', type=float, default=0.1,
                        help='Minimum energy in eV of Spencer-Fano solution')

    parser.add_argument('-emax', type=float, default=16000,
                        help='Maximum energy in eV of Spencer-Fano solution (approx where energy is injected)')

    parser.add_argument('-vary', action='store', choices=['emin', 'emax', 'npts', 'emax,npts'],
                        help='Which parameter to vary')

    parser.add_argument('--workfn', action='store_true',
                        help='Testing related to work functions and high energy limits')

    parser.add_argument('--makeplot', action='store_true',
                        help='Save a plot of the non-thermal spectrum')

    parser.add_argument('--differentialform', action='store_true',
                        help=('Solve differential form (KF92 Equation 6) instead of'
                              'integral form (KF92 Equation 7)'))

    parser.add_argument('--noexcitation', action='store_true',
                        help='Do not include collisional excitation transitions')

    parser.add_argument('--ar1985', action='store_true',
                        help='Use Arnaud & Rothenflug (1985, A&AS, 60, 425) for Fe ionization cross sections')

    parser.add_argument('-o', action='store', dest='outputfile',
                        default=defaultoutputfile,
                        help='Path/filename for PDF file if --makeplot is enabled')

    parser.add_argument('-ostat', action='store',
                        help='Path/filename for stats output')


def main(args=None, argsraw=None, **kwargs):
    if args is None:
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            description='Plot estimated spectra from bound-bound transitions.')
        addargs(parser)
        parser.set_defaults(**kwargs)
        args = parser.parse_args(argsraw)

    modelpath = Path(args.modelpath)

    if args.workfn:
        return workfunction_tests(modelpath, args)

    if Path(args.outputfile).is_dir():
        args.outputfile = Path(args.outputfile, defaultoutputfile)

    # if args.timedays:
    #     args.timestep = at.get_timestep_of_timedays(modelpath, args.timedays)
    # elif args.timestep is None:
    #     print("A time or timestep must be specified.")
    #     sys.exit()
    #
    # modeldata, _ = at.get_modeldata(modelpath)
    # if args.velocity >= 0.:
    #     args.modelgridindex = at.get_mgi_of_velocity_kms(modelpath, args.velocity)
    # else:
    #     args.modelgridindex = args.modelgridindex
    # estimators = at.estimators.read_estimators(modelpath, timestep=args.timestep, modelgridindex=args.modelgridindex)
    # estim = estimators[(args.timestep, args.modelgridindex)]
    #
    # dfpops = at.nltepops.read_files(modelpath, modelgridindex=args.modelgridindex, timestep=args.timestep)
    #
    # if dfpops is None or dfpops.empty:
    #     print(f'ERROR: no NLTE populations for cell {args.modelgridindex} at timestep {args.timestep}')
    #     return -1
    #
    # nntot = estim['populations']['total']
    # nne = estim['nne']
    # deposition_density_ev = estim['heating_dep'] / 1.6021772e-12  # convert erg to eV
    # ionpopdict = estim['populations']

    # velocity = modeldata['velocity_outer'][args.modelgridindex]
    # args.timedays = float(at.get_timestep_time(modelpath, args.timestep))
    # print(f'timestep {args.timestep} cell {args.modelgridindex} (v={velocity} km/s at {args.timedays:.1f}d)')

    # ionpopdict = {}
    # deposition_density_ev = 327
    # nne = 6.7e5
    #
    # ionpopdict[(26, 1)] = ionpopdict[26] * 1e-4
    # ionpopdict[(26, 2)] = ionpopdict[26] * 0.20
    # ionpopdict[(26, 3)] = ionpopdict[26] * 0.80
    # ionpopdict[(26, 4)] = ionpopdict[26] * 0.
    # ionpopdict[(26, 5)] = ionpopdict[26] * 0.
    # ionpopdict[(27, 2)] = ionpopdict[27] * 0.20
    # ionpopdict[(27, 3)] = ionpopdict[27] * 0.80
    # ionpopdict[(27, 4)] = 0.
    # # ionpopdict[(28, 1)] = ionpopdict[28] * 6e-3
    # ionpopdict[(28, 2)] = ionpopdict[28] * 0.18
    # ionpopdict[(28, 3)] = ionpopdict[28] * 0.82
    # ionpopdict[(28, 4)] = ionpopdict[28] * 0.
    # ionpopdict[(28, 5)] = ionpopdict[28] * 0.

    x_e = 2.
    deposition_density_ev = 1e2
    nntot = 1.0e5
    ionpopdict = {}
    nne = nntot * x_e
    # nne = .1
    dfpops = {}

    ionpopdict[(at.get_atomic_number('Fe'), 2)] = nntot * 1.
    # ionpopdict[(at.get_atomic_number('Fe'), 3)] = nntot * 0.5

    # KF1992 D. The Oxygen-Carbon Zone
    # ionpopdict[(at.get_atomic_number('C'), 1)] = 0.16 * nntot
    # ionpopdict[(at.get_atomic_number('C'), 2)] = 0.16 * nntot * x_e
    # ionpopdict[(at.get_atomic_number('O'), 1)] = 0.86 * nntot
    # ionpopdict[(at.get_atomic_number('O'), 2)] = 0.86 * nntot * x_e
    # ionpopdict[(at.get_atomic_number('Ne'), 1)] = 0.016 * nntot

    # # KF1992 G. The Silicon-Calcium Zone
    # ionpopdict[(at.get_atomic_number('C'), 1)] = 0.38e-5 * nntot
    # ionpopdict[(at.get_atomic_number('O'), 1)] = 0.94e-4 * nntot
    # ionpopdict[(at.get_atomic_number('Si'), 1)] = 0.63 * nntot
    # ionpopdict[(at.get_atomic_number('Si'), 2)] = 0.63 * nntot * x_e
    # ionpopdict[(at.get_atomic_number('S'), 1)] = 0.29 * nntot
    # ionpopdict[(at.get_atomic_number('S'), 2)] = 0.29 * nntot * x_e
    # ionpopdict[(at.get_atomic_number('Ar'), 1)] = 0.041 * nntot
    # ionpopdict[(at.get_atomic_number('Ca'), 1)] = 0.026 * nntot
    # ionpopdict[(at.get_atomic_number('Fe'), 1)] = 0.012 * nntot

    ions = []
    for key in ionpopdict.keys():
        # keep only the ion populations, not element or total populations
        if isinstance(key, tuple) and len(key) == 2 and ionpopdict[key] / nntot >= minionfraction:
            if key[0] >= 26:  # TODO: remove
                ions.append(key)

    ions.sort()

    if args.noexcitation:
        adata = None
        dfpops = None
    else:
        adata = at.get_levels(modelpath, get_transitions=True, ionlist=tuple(ions))
        dfpops = get_lte_pops(adata, ions, ionpopdict, temperature=6000)
    nnetot = get_nnetot(ions, ionpopdict)
    print(f'     nntot: {nntot:.2e} /cm3')
    print(f'       nne: {nne:.2e} /cm3')
    print(f'    nnetot: {nnetot:.2e} /cm3')
    print(f'deposition: {deposition_density_ev:7.2f} eV/s/cm3')

    dfcollion = at.nonthermal.read_colliondata(
        collionfilename=('collion-AR1985.txt' if args.ar1985 else 'collion.txt'))

    if args.ostat:
        with open(args.ostat, 'w') as fstat:
            fstat.write('emin emax npts FeII_frac_ionization FeII_frac_excitation FeII_gamma_nt '
                        'NiII_frac_ionization NiII_frac_excitation NiII_gamma_nt\n')

    stepcount = 20 if args.vary else 1
    for step in range(stepcount):
        emin = args.emin
        emax = args.emax
        npts = args.npts
        if args.vary == 'emin':
            emin *= 2 ** step
        elif args.vary == 'emax':
            emax *= 2 ** step
        elif args.vary == 'npts':
            npts *= 2 ** step
        if args.vary == 'emax,npts':
            npts *= 2 ** step
            emax *= 2 ** step
        engrid = np.linspace(emin, emax, num=npts, endpoint=True)
        deltaen = engrid[1] - engrid[0]

        sourcevec = np.zeros(engrid.shape)
        # source_spread_pts = math.ceil(npts / 10.)
        source_spread_pts = math.ceil(npts * 0.03)
        for s in range(npts):
            # spread the source over some energy width
            if (s < npts - source_spread_pts):
                sourcevec[s] = 0.
            elif (s < npts):
                sourcevec[s] = 1. / (deltaen * source_spread_pts)
        # sourcevec[-1] = 1.
        # sourcevec[-3] = 1.

        if args.differentialform:
            yvec, dftransitions = solve_spencerfano_differentialform(
                ions, ionpopdict, dfpops, nne, deposition_density_ev, engrid, sourcevec, dfcollion, args,
                adata=adata, noexcitation=args.noexcitation)
        else:
            yvec, dftransitions = solve_spencerfano(
                ions, ionpopdict, dfpops, nne, deposition_density_ev, engrid, sourcevec, dfcollion, args,
                adata=adata, noexcitation=args.noexcitation)

        if args.makeplot:
            # outputfilename = str(args.outputfile).format(
            #     cell=args.modelgridindex, timestep=args.timestep, timedays=args.timedays)
            outputfilename = 'spencerfano.pdf'
            make_plot(engrid, yvec, ions, ionpopdict, dfcollion, dftransitions, nne, nnetot, sourcevec,
                      deposition_density_ev, outputfilename, noexcitation=args.noexcitation)

        (frac_excitation, frac_ionization, frac_excitation_ion, frac_ionization_ion, gamma_nt) = analyse_ntspectrum(
            engrid, yvec, ions, ionpopdict, nntot, nne, deposition_density_ev,
            dfcollion, dftransitions, noexcitation=args.noexcitation, modelpath=modelpath)

        if args.ostat:
            with open(args.ostat, 'a') as fstat:
                fstat.write(f'{emin} {emax} {npts} {frac_ionization_ion[(26, 2)]:.4f} '
                            f'{frac_excitation_ion[(26, 2)]:.4f} '
                            f'{gamma_nt[(26, 2)]:.4e} {frac_ionization_ion[(28, 2)]:.4f} '
                            f'{frac_excitation_ion[(28, 2)]:.4f} {gamma_nt[(28, 2)]:.4e}\n')


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
