#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Nov  9 14:30:10 2021

______________________________

|      Neo-Mayo estimator     |
______________________________


@author: sand-jrd
"""

# For file management
from neo_mayo.utils import unpack_science_datadir
from vip_hci.fits import open_fits, write_fits
from vip_hci.preproc import cube_derotate

from os import mkdir
from os.path import isdir

# Algo and science model
from neo_mayo.algo import init_estimate, sobel_tensor_conv
import torch
import numpy as np


# Loss functions
import torch.optim as optim
from torch import sum as tsum

# Other
from neo_mayo.utils import circle, iter_to_gif, print_iter
from neo_mayo.model import model_ADI

# -- For verbose -- #
from datetime import datetime
import warnings

sep = ('_' * 50)

info_iter = "Iteration n°{} : total loss {:.12e}" +\
            "\n\tWith R1 = {:.7e} ({:.0f}%)" +\
            "\n\tWith R2 = {:.7e} ({:.0f}%)" +\
            "\n\tand loss = {:.7e} ({:.0f}%)"

init_msg = sep + "\nResolving IP-ADI optimization problem ...{}" +\
                 "\nProcessing cube from : {}" +\
                 "\nRegul R1 : '{}' and R2 : '{}'" +\
                 "\n{} deconvolution and {} frame weighted based on rotations" +\
                 "\nw_r are sets to w_r={:.2e} and w_r2={:.2e}, maxiter={}\n"

activ_msg = "REGUL HAVE BEEN {} with w_r={:.2e} and w_r2={:.2e}"


def loss_ratio(Ractiv: int or bool, R1: float, R2: float, L: float) -> tuple:
    """ Compute Regul weight over data attachment terme """
    return tuple(np.array((1, 100/L)) * abs(R1)) + \
           tuple(np.array((1, 100/L)) * abs(R2)) + \
           tuple(np.array((1, 100/L)) * (L - Ractiv * (abs(R1) + abs(R2))))


# %% ------------------------------------------------------------------------
class mayo_estimator:
    """ Neo-mayo Algorithm main class  """

    def __init__(self, datadir="./data", coro=6, pupil="edge", ispsf=False, weighted_rot=True, regul="smooth",
                 Badframes=None, epsi=1e-3):
        """
        Initialisation of estimator object

        Parameters
        ----------
        datadir : str
            path to data. In this folder, it must find a json file.
            The json should tell the fits file name of cube and angles.
            Json tempalte can be found in exemple-data/0_import_info.json
        coro : int
            size of the coronograph
        pupil : int, None or "edge"
            Size of the pupil.
            If pupil is set to "edge" : the pupil raduis will be half the size of the frame
            If pupil is set to None : there will be no pupil at all
        ispsf : bool
            if True, will perform deconvolution (i.e conv by psf inculded in forward model)
            /!\ "psf" key must be added in json import file
        weighted_rot : bool
            if True, each frame will be weighted by the delta of rotation.
            see neo-mayo thechnial details to know more.
        regul : str (WILL MAAYYYBE BE REMOVE BECAUSE USELESS)
            R1 regularization mode {'smooth', 'smooth_with_edges', 'l1'}
            The R1 will be applied on X (disk and planet contribution)
        Badframes : tuple or list or None
            Bad frames that you will not be taken into account
        epsi : (WILL MAYBE BE REMOVE BECAUSE USELESS, IT IS NEVER SHAPE LOL) float
            'smooth_with_edges' parameters
        """

        # -- Create model and define constants --
        angles, science_data, psf =  unpack_science_datadir(datadir, ispsf)

        if Badframes is not None:
            science_data = np.delete(science_data, Badframes, 0)
            angles = np.delete(angles, Badframes, 0)

        # Constants
        self.shape = science_data[0].shape
        self.nb_frames = science_data.shape[0]
        self.L0x0 = None
        self.mask = 1  # R2 mask default. Will be set if call R2 config
        self.name = datadir

        # Coro and pupil masks
        if pupil is None: pupil = np.ones(self.shape); pupilR = pupil
        elif pupil == "edge":
            pupil  = circle(self.shape, self.shape[0]/2)
            pupilR = circle(self.shape, self.shape[0]/2 - 2)
        elif isinstance(pupil, float) or isinstance(pupil, int):
            pupil  = circle(self.shape, pupil)
            pupilR = circle(self.shape, pupil - 3)
        else: raise ValueError("Invalid pupil key argument. Possible values : {float/int, None, 'edge'}")

        if coro is None: coro = 0
        self.coro   = (1 - circle(self.shape, coro)) * pupil
        self.coroR  = (1 - circle(self.shape, coro+3)) * pupilR

        # Convert to tensor
        rot_angles = normlizangle(angles)
        self.science_data = science_data
        self.model = model_ADI(rot_angles, self.coro, psf)

        # Compute ponderation weight by rotations
        ang_weight = compute_rot_weight(rot_angles) if weighted_rot else np.ones(self.nb_frames)
        self.ang_weight = torch.from_numpy(ang_weight.reshape((self.nb_frames, 1, 1, 1))).double()

        self.coro  = torch.from_numpy(self.coro).double()
        self.coroR = torch.from_numpy(self.coroR).double()

        # -- Define our minimization problem

        if regul == "smooth_with_edges":
            self.regul = lambda X: torch.sum(self.coroR * sobel_tensor_conv(X) ** 2 - epsi ** 2)
        elif regul == "smooth":
            self.regul = lambda X: torch.sum(self.coroR * sobel_tensor_conv(X, axis='y') ** 2) +\
                                   torch.sum(self.coroR * sobel_tensor_conv(X, axis='x') ** 2)
        elif regul == "l1":
            self.regul = lambda X: torch.sum(self.coroR * torch.abs(X))

        self.regul2 = lambda X, L, M:  0

        # Config list : [R1, R2, ispsf, is wieghted_rot]
        self.config = [regul, None, 'With' if ispsf else 'No', 'with' if weighted_rot else 'no']
        self.res = None; self.last_iter = None; self.first_iter = None; self.final_estim = None  # init results vars

    def initialisation(self, from_dir=None, save=None, Imode="max_common",  **kwargs):
        """ Init L0 (starlight) and X0 (circonstellar) with a PCA
         
         Parameters
         ----------
          from_dir : str (optional)
             if None, compute PCA
             if a path is given, get L0 (starlight) and X0 (circonstellar) from the directory

          save : str (optional)
             if a path is given, L0 (starlight) and X0 (circonstellar) will be saved at the given emplacement

          Imode : str
            Type of initialisation : {'pcait','pca'}

          kwargs :
             Argument that will be pass to :
             vip_hci.pca.pca_fullfr.pca if 'Imode' = 'pca'
             vip_hci.itpca.pca_it if 'Imode' = 'pcait'

         Returns
         -------
         L_est, X_est: ndarray
             Estimated starlight (L) 
             and circunstlellar contributions (X) 
         
        """

        if from_dir:
            print("Get init from pre-processed datas...")
            L0 = open_fits(from_dir + "/L0.fits", verbose=False)
            X0 = open_fits(from_dir + "/X0.fits", verbose=False)

        else:

            # -- Iterative PCA for variable init -- #
            start_time = datetime.now()
            print(sep + "\nInitialisation  ...")

            L0, X0 = init_estimate(self.science_data, self.model.rot_angles, Imode=Imode , **kwargs)
            #X0 = gaussian_filter(X0, sigma=1)

            print("Done - running time : " + str(datetime.now() - start_time) + "\n" + sep)

            if save:
                if not isdir(save): mkdir(save)
                print("Save init from in " + save + "...")
                write_fits(save + "/L0.fits", L0, verbose=False)
                write_fits(save + "/X0.fits", X0, verbose=False)

        # -- Define constantes
        self.L0x0 = (L0, X0)

        return L0, X0

    def configR2(self, Msk=None, mode = "mask", penaliz = "X", invert=False):
        """ Configuration for Second regularization.
        Two possible mode :   R = M*X      (mode 'mask')
                           or R = dist(M-X) (mode 'dist')
                           or R = sum(X)   (mode 'l1')

        Parameters
        ----------
        Msk : numpy.array or torch.tensor
            Prior mask of X.

        mode : {"mask","dist"}.
            if "mask" :  R = norm(M*X)
                if M will be normalize to have values from 0 to 1.
            if "dist" :  R = dist(M-X)

        penaliz : {"X","L","both"}
            Unknown to penaliz.
            With mode mask :
                if "X" : R = norm(M*X)
                if "L" : R = norm((1-M)*L)
                if "B" : R = norm(M*X) + norm((1-M)*L)
            With mode dist :
                if "X" : R = dist(M-X)
                if "L" : R = dist(M-L)
                if "B" : R = dist(M-X) - dist(M-L)
            With mode l1 :
                if "X" : R = sum(X)
                if "L" : R = sum(L)
                if "B" : R = sum(X) - sum(L)

        invert : Bool
            Reverse penalize mode bewteen L and X.

        Returns
        -------

        """
        if mode != 'l1' :
            if isinstance(Msk, np.ndarray): Msk = torch.from_numpy(Msk)
            if not (isinstance(Msk, torch.Tensor) and Msk.shape == self.model.frame_shape):
                raise TypeError("Mask M should be tensor or arr y of size " + str(self.model.frame_shape))

        if Msk is not None and mode != 'mask' :
            warnings.warn("You give a mask but didn't use 'mask' option.", UserWarning, )

        penaliz = penaliz.capitalize()
        rM = self.coroR  # corono mask for regul
        if penaliz not in ("X", "L", "Both", "B") :
            raise Exception("Unknown value of penaliz. Possible values are {'X','L','B'}")

        if mode == "dist":
            self.mask = Msk
            sign = -1 if invert else 1
            if   penaliz == "X"   :  self.regul2 = lambda X, L, M: tsum( rM * (M - X) ** 2)
            elif penaliz == "L"   :  self.regul2 = lambda X, L, M: tsum( rM * (M - X) ** 2)
            elif penaliz in ("Both", "B"):  self.regul2 = lambda X, L, M: sign * (tsum( rM * (M - X) ** 2) -
                                                                                  tsum( rM * (M - L) ** 2))

        elif mode == "mask":
            Msk = Msk/torch.max(Msk)  # Normalize mask
            self.mask = (1-Msk) if invert else Msk
            if   penaliz == "X"   : self.regul2 = lambda X, L, M: tsum( rM * (M * X) ** 2)
            elif penaliz == "L"   : self.regul2 = lambda X, L, M: tsum( rM * ((1 - M) * L) ** 2)
            elif penaliz in ("Both", "B"): self.regul2 = lambda X, L, M: tsum( rM * (M * X) ** 2) +\
                                                                  tsum(M)**2/tsum((1 - M))**2 *\
                                                                  tsum( rM * ((1 - M) * L) ** 2)

        elif mode == "l1":
            sign = -1 if invert else 1
            if   penaliz == "X"   : self.regul2 = lambda X, L, M: tsum(X ** 2)
            elif penaliz == "L"   : self.regul2 = lambda X, L, M: tsum(L ** 2)
            elif penaliz in ("Both", "B"): self.regul2 = lambda X, L, M: sign * (tsum(X ** 2) - tsum(L ** 2))

        else: raise Exception("Unknown value of mode. Possible values are {'mask','dist','l1'}")

        R2_name = mode + " on " + penaliz
        R2_name += " inverted" if invert else ""
        self.config[1] = R2_name

    def estimate(self, w_r=0.03, w_r2=0.03, w_pcent=True, estimI=False, maxiter=10, w_way=(0, 1),
                 gtol=1e-10, kactiv=0, kdactiv=None, save=True, suffix = "", gif=False, verbose=False):
        """ Resole the minimization of probleme neo-mayo
            The first step with pca aim to find a good initialisation
            The second step process to the minimization
         
        Parameters
        ----------

        w_r : float
            Weight regularization, hyperparameter to control R1 regularization (smooth regul)

        w_r2 : float
            Weight regularization, hyperparameter to control R2 regularization (mask regul)

        w_pcent : bool
            Determine if regularization weight are raw value or percentage
            Pecentage a computed based on either the initaial values of L and X with PCA
            If kactive is set, compute the value based on the L and X at activation.

        estimI : bool
            If True, estimate flux variation between each frame of data-science cube I_ks.
            ADI model will be written as I_k*L + R(X) = Y_k

        maxiter : int
            Maximum number of optimization steps

        gtol : float
            Gradient tolerance; Set the break point of minimization.
            The break point is define as abs(J_n - J-(n-1)) < gtol

        kactiv : int or {"converg"}
            Activate regularization at a specific iteration
            If set to "converg", will be activated when break point is reach
            Equivalent to re-run a minimization with regul and L and X initialized at converged value

        kdactiv : int or {"converg"}
            Deactivate regularization at a specific iteration
            If set to "converg", will be deactivated when break point is reach
            Equivalent to re-run a minimization with no regul and L and X initialized at converged value

        save : str or None
            if path is given, save the result as fits

        suffix : string
            String suffix to named the simulation outputs

        gif : bool
            if path is given, save each the minimization step as a gif

        verbose : bool
            Activate or deactivate print

         Returns
         -------
         L_est, X_est: ndarray
             Estimated starlight (L) and circunstlellar (X) contributions
         
        """

        if self.L0x0 is None:
            print("You didn't run initialization : start with L0=mean(cube), X0=mean(cube-L0)")
            self.L0x0 = abs(np.mean(self.science_data, 0)),\
                        np.mean((self.science_data-np.mean(self.science_data, 0)).clip(min=0), 0)
        mink  = 2  # Min number of iter before convergence
        loss_evo = []; grad_evo = []

        # Regularization activation init setting
        if kactiv == "converg" : kactiv = maxiter  # If option converge, kactiv start when miniz end
        Ractiv = 0 if kactiv else 1  # Ractiv : regulazation is curently activated or not
        w_rp = w_r, w_r2 if w_pcent else  0, 0

        # ______________________________________
        # Define constantes and convert arry to tensor
        self.science_data = (self.science_data - np.median(self.science_data)).clip(0)
        science_data_derot = cube_derotate(self.science_data, self.model.rot_angles)
        science_data_derot = torch.unsqueeze(torch.from_numpy(science_data_derot), 1).double()
        science_data = torch.unsqueeze(torch.from_numpy(self.science_data), 1).double()
        science_data.requires_grad = False; science_data_derot.requires_grad = False


        L0, X0 = self.L0x0

        L0 = torch.unsqueeze(torch.from_numpy(L0), 0).double()
        X0 = torch.unsqueeze(torch.from_numpy(X0), 0).double()
        flux_0 = torch.ones(self.model.nb_frame - 1)

        # ______________________________________
        #  Init variables and optimizer

        Lk, Xk, flux_k, k = L0.clone(), X0.clone(), flux_0.clone(), 0
        Lk.requires_grad = True; Xk.requires_grad = True

        # Model and loss at step 0
        with torch.no_grad():

            Y0 = self.model.forward_ADI(L0, X0, flux_0) if w_way[0] else 0
            Y0_reverse = self.model.forward_ADI_reverse(L0, torch.flip(X0, [2]), flux_0) if w_way[1] else 0

            loss0 = w_way[0] * torch.sum(self.ang_weight * self.coro * (Y0 - science_data) ** 2) + \
                    w_way[1] * torch.sum(self.ang_weight * self.coro * (Y0_reverse - science_data_derot) ** 2)

            if w_pcent and Ractiv :
                w_r  = w_rp[0] * loss0 / self.regul(X0)   # Auto hyperparameters
                w_r2 = w_rp[1] * loss0 / self.regul2(X0, L0, self.mask)

            R1_0 = Ractiv * w_r * self.regul(X0)
            R2_0 = Ractiv * w_r2 * self.regul2(X0, L0, self.mask)
            loss0 += (R1_0 + R2_0)

        loss, R1, R2 = loss0, R1_0, R2_0

        # ____________________________________
        # Nested functions

        # Definition of minimizer step.
        def closure():
            nonlocal R1, R2, loss, w_r, w_r2, Lk, Xk, flux_k
            optimizer.zero_grad()  # Reset gradients

            # Compute model(s)
            Yk = self.model.forward_ADI(Lk, Xk, flux_k) if w_way[0] else 0
            Yk_reverse = self.model.forward_ADI_reverse(Lk, Xk, flux_k) if w_way[1] else 0

            # Compute regularization(s)
            R1 = Ractiv * w_r * self.regul(Xk)
            R2 = Ractiv * w_r2 * self.regul2(Xk, Lk, self.mask)

            # Compute loss and local gradients
            loss = w_way[0] * torch.sum( self.ang_weight * self.coro * (Yk - science_data) ** 2) + \
                   w_way[1] * torch.sum( self.ang_weight * self.coro * (Yk_reverse - science_data_derot) ** 2) + \
                   (R1 + R2)

            loss.backward()
            return loss

        # Definition of regularization activation
        def activation():
            nonlocal w_r, w_r2, optimizer, Xk, Lk, flux_k
            for activ_step in ["ACTIVATED", "AJUSTED"]:  # Activation in two step

                # Second step : re-compute regul after performing a optimizer step
                if activ_step == "AJUSTED": optimizer.step(closure)

                with torch.no_grad():
                    if w_pcent:
                        w_r  = w_rp[0] * loss / self.regul(Xk)
                        w_r2 = w_rp[1] * loss / self.regul2(Xk, Lk, self.mask)
                if estimI:
                    optimizer = optim.LBFGS([Lk, Xk, flux_k])
                    flux_k.requires_grad = True
                else:
                    optimizer = optim.LBFGS([Lk, Xk])

                sub_iter = 0 if activ_step == "ACTIVATED" else 0.5
                process_to_prints(activ_msg.format(activ_step, w_r, w_r2), sub_iter)

        # Definition of print routines
        def process_to_prints(extra_msg=None, sub_iter=0):
            iter_msg = info_iter.format(k + sub_iter, loss, *loss_ratio(Ractiv, float(R1), float(R2), float(loss)))
            est_info = stat_msg.split("\n", 4)[3] + '\n'
            if gif: print_iter(Lk, Xk, flux_k, k + sub_iter, est_info + iter_msg, extra_msg, save, self.coro)
            if extra_msg: iter_msg += "\n" + extra_msg
            if verbose: print(iter_msg)

        # If L_I is set to True, L variation intensity will be estimated
        if estimI :
            optimizer = optim.LBFGS([Lk, Xk, flux_k])
            flux_k.requires_grad = True
        else:
            optimizer = optim.LBFGS([Lk, Xk])

        # Starting minization soon ...
        stat_msg = init_msg.format(self.name, suffix, *self.config, w_r, w_r2, str(maxiter))
        if verbose: print(stat_msg)

        # Save & prints the first iteration
        loss_evo.append(loss)
        self.last_iter = (L0, X0, flux_0) if estimI else (L0, X0)
        process_to_prints()

        start_time = datetime.now()

        # ____________________________________
        # ------ Minimization Loop ! ------ #

        try :
            for k in range(1, maxiter+1):

                # Activation
                if kactiv and k == kactiv:
                    Ractiv = 1; mink = k + 2
                    activation()

                # Descactivation
                if kdactiv and k == kdactiv: Ractiv = 0

                # -- MINIMIZER STEP -- #
                optimizer.step(closure)
                if k > 1 and loss > loss_evo[-1]: self.final_estim = self.last_iter

                # Save & prints
                loss_evo.append(loss)
                grad_evo.append(torch.mean(abs(Lk.grad.data)) + torch.mean(abs(Xk.grad.data)))
                self.last_iter = (Lk, Xk, flux_k) if estimI else (Lk, Xk)
                if k==1 : self.first_iter = (Lk, Xk, flux_k) if estimI else (Lk, Xk)
                process_to_prints()

                # Break point (based on gtol)
                if k > mink and torch.mean(abs(Lk.grad.data)) < gtol and torch.mean(abs(Xk.grad.data)) < gtol:
                    if not Ractiv and kactiv:  # If regul haven't been activated yet, continue with regul
                        Ractiv = 1; mink = k+2
                        activation()
                    else : break

            ending = 'max iter reached' if k == maxiter else 'gtol reached'

        except KeyboardInterrupt:
            ending = "keyboard interruption"

        print("Done ("+ending+") - Running time : " + str(datetime.now() - start_time))

        # ______________________________________
        # Done, store and unwrap results back to numpy array!

        if k > 1 and loss > loss_evo[-2] and self.final_estim is not None:
            L_est = abs(self.final_estim[0].detach().numpy()[0])
            X_est = abs((self.coro * self.final_estim[1]).detach().numpy()[0])
        else :
            L_est, X_est = abs(Lk.detach().numpy()[0]), abs((self.coro * Xk).detach().numpy()[0])

        flux  = abs(flux_k.detach().numpy())

        # Result dict
        res = {'state'   : optimizer.state,
               'x'       : (L_est, X_est),
               'flux'    : flux,
               'loss_evo': loss_evo,
               'ending'  : ending}

        self.res = res

        # Save
        if gif : iter_to_gif(save, suffix)

        suffix = '' if not suffix else '_' + suffix
        if save: write_fits(save + "/L_est"+suffix, L_est), write_fits(save + "/X_est"+suffix, X_est)
        if save and estimI : write_fits(save + "/flux"+suffix, flux)

        if estimI: return L_est, X_est, flux
        else     : return L_est, X_est


    def get_science_data(self):
        """Return input cube and angles"""
        return self.model.rot_angles, self.science_data

# %% ------------------------------------------------------------------------
def normlizangle(angles: np.array) -> np.array:
    """Normaliz an angle between 0 and 360"""

    angles[angles < 0] += 360
    angles = angles % 360

    return angles

def compute_rot_weight(angs: np.array) -> np.array:
    nb_frm = len(angs)

    # Set value of the delta offset max
    max_rot = np.median(abs(angs[:-1]-angs[1:]))
    if max_rot > 1 : max_rot = 1

    ang_weight = []
    for id_ang, ang in enumerate(angs):
        # If a frame neighbours delta-rot under max_rot, frames are less valuable than other frame (weight<1)
        # If the max_rot is exceed, the frame is not consider not more valuable than other frame (weight=1)
        # We do this for both direction's neighbourhoods

        nb_neighbours = 0; detla_ang = 0
        tmp_id = id_ang

        while detla_ang < max_rot and tmp_id < nb_frm - 1:  # neighbours after
            tmp_id += 1; nb_neighbours += 1
            detla_ang += abs(angs[tmp_id] - angs[id_ang])
        w_1 = (1 + abs(angs[id_ang] - angs[id_ang + 1])) / nb_neighbours if nb_neighbours > 1 else 1

        tmp_id = id_ang; detla_ang = 0
        nb_neighbours = 0
        while detla_ang < max_rot and tmp_id > 0:  # neighbours before
            tmp_id -= 1; nb_neighbours += 1
            detla_ang += abs(angs[tmp_id] - angs[id_ang])
        w_2 = (1 + abs(angs[id_ang] - angs[id_ang - 1])) / nb_neighbours if nb_neighbours > 1 else 1

        ang_weight.append((w_2 + w_1) / 2)

    return np.array(ang_weight)*(nb_frm/sum(ang_weight))
