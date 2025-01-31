import logging
import json
import numpy as np
import time
import warnings

from copy import deepcopy
from datetime import datetime
from shutil import copyfile
from typing import List, Tuple, Union

import flare
import flare.predict as predict
from flare import struc, gp, env, md
from flare.dft_interface import dft_software
from flare.output import Output
from flare.utils.learner import is_std_in_bound
from flare.utils.element_coder import NumpyEncoder


class OTF:
    """Trains a Gaussian process force field on the fly during
        molecular dynamics.

    Args:
        dt (float): MD timestep.
        number_of_steps (int): Number of timesteps in the training
            simulation.
        prev_pos_init ([type], optional): Previous positions. Defaults
            to None.
        rescale_steps (List[int], optional): List of frames for which the
            velocities of the atoms are rescaled. Defaults to [].
        rescale_temps (List[int], optional): List of rescaled temperatures.
            Defaults to [].

        gp (gp.GaussianProcess): Initial GP model.
        calculate_energy (bool, optional): If True, the energy of each
            frame is calculated with the GP. Defaults to False.
        calculate_efs (bool, optional): If True, the energy and stress of each
            frame is calculated with the GP. Defaults to False.
        write_model (int, optional): If 0, write never. If 1, write at
            end of run. If 2, write after each training and end of run.
            If 3, write after each time atoms are added and end of run.
        force_only (bool, optional): If True, only use forces for training.
            Default to False, use forces, energy and stress for training.

        std_tolerance_factor (float, optional): Threshold that determines
            when DFT is called. Specifies a multiple of the current noise
            hyperparameter. If the epistemic uncertainty on a force
            component exceeds this value, DFT is called. Defaults to 1.
        skip (int, optional): Number of frames that are skipped when
            dumping to the output file. Defaults to 0.
        init_atoms (List[int], optional): List of atoms from the input
            structure whose local environments and force components are
            used to train the initial GP model. If None is specified, all
            atoms are used to train the initial GP. Defaults to None.
        output_name (str, optional): Name of the output file. Defaults to
            'otf_run'.
        max_atoms_added (int, optional): Number of atoms added each time
            DFT is called. Defaults to 1.
        freeze_hyps (int, optional): Specifies the number of times the
            hyperparameters of the GP are optimized. After this many
            updates to the GP, the hyperparameters are frozen.
            Defaults to 10.
        min_steps_with_model (int, optional): Minimum number of steps the
            model takes in between calls to DFT. Defaults to 0.
        force_source (Union[str, object], optional): DFT code used to calculate
            ab initio forces during training. A custom module can be used here
            in place of the DFT modules available in the FLARE package. The
            module must contain two functions: parse_dft_input, which takes a
            file name (in string format) as input and returns the positions,
            species, cell, and masses of a structure of atoms; and run_dft_par,
            which takes a number of DFT related inputs and returns the forces
            on all atoms.  Defaults to "qe".
        npool (int, optional): Number of k-point pools for DFT
            calculations. Defaults to None.
        mpi (str, optional): Determines how mpi is called. Defaults to
            "srun".
        dft_loc (str): Location of DFT executable.
        dft_input (str): Input file.
        dft_output (str): Output file.
        dft_kwargs ([type], optional): Additional arguments which are
            passed when DFT is called; keyword arguments vary based on the
            program (e.g. ESPRESSO vs. VASP). Defaults to None.
        store_dft_output (Tuple[Union[str,List[str]],str], optional):
            After DFT calculations are called, copy the file or files
            specified in the first element of the tuple to a directory
            specified as the second element of the tuple.
            Useful when DFT calculations are expensive and want to be kept
            for later use. The first element of the tuple can either be a
            single file name, or a list of several. Copied files will be
            prepended with the date and time with the format
            'Year.Month.Day:Hour:Minute:Second:'.

        n_cpus (int, optional): Number of cpus used during training.
            Defaults to 1.
    """

    def __init__(
        self,
        # md args
        dt: float,
        number_of_steps: int,
        prev_pos_init: "ndarray" = None,
        rescale_steps: List[int] = [],
        rescale_temps: List[int] = [],
        # flare args
        gp: gp.GaussianProcess = None,
        calculate_energy: bool = False,
        calculate_efs: bool = False,
        write_model: int = 0,
        force_only: bool = True,
        # otf args
        std_tolerance_factor: float = 1,
        skip: int = 0,
        init_atoms: List[int] = None,
        output_name: str = "otf_run",
        max_atoms_added: int = 1,
        freeze_hyps: int = 10,
        min_steps_with_model: int = 0,
        update_style: str = "add_n",
        update_threshold: float = None,
        # dft args
        force_source: str = "qe",
        npool: int = None,
        mpi: str = "srun",
        dft_loc: str = None,
        dft_input: str = None,
        dft_output="dft.out",
        dft_kwargs=None,
        store_dft_output: Tuple[Union[str, List[str]], str] = None,
        # other args
        n_cpus: int = 1,
        **kwargs,
    ):

        # set DFT
        self.dft_loc = dft_loc
        self.dft_input = dft_input
        self.dft_output = dft_output
        self.dft_step = True
        self.dft_count = 0
        if isinstance(force_source, str):
            self.dft_module = dft_software[force_source]
        else:
            self.dft_module = force_source

        # set md
        self.dt = dt
        self.number_of_steps = number_of_steps
        self.get_structure_from_input(prev_pos_init)  # parse input file
        self.noa = self.structure.positions.shape[0]
        self.rescale_steps = rescale_steps
        self.rescale_temps = rescale_temps

        # set flare
        self.gp = gp
        # initialize local energies
        if calculate_energy:
            self.local_energies = np.zeros(self.noa)
        else:
            self.local_energies = None
        self.force_only = force_only

        # set otf
        self.std_tolerance = std_tolerance_factor
        self.skip = skip
        self.max_atoms_added = max_atoms_added
        self.freeze_hyps = freeze_hyps
        if init_atoms is None:  # set atom list for initial dft run
            self.init_atoms = [int(n) for n in range(self.noa)]
        else:
            self.init_atoms = init_atoms
        self.update_style = update_style
        self.update_threshold = update_threshold

        self.n_cpus = n_cpus  # set number of cpus and npool for DFT runs
        self.npool = npool
        self.mpi = mpi
        self.min_steps_with_model = min_steps_with_model

        self.dft_kwargs = dft_kwargs
        self.store_dft_output = store_dft_output

        # other args
        self.atom_list = list(range(self.noa))
        self.curr_step = 0
        self.steps_since_dft = 0

        # Set the prediction function based on user inputs.
        # Force only prediction.
        if (n_cpus > 1 and gp.per_atom_par and gp.parallel) and not (
            calculate_energy or calculate_efs
        ):
            self.pred_func = predict.predict_on_structure_par
        elif not (calculate_energy or calculate_efs):
            self.pred_func = predict.predict_on_structure
        # Energy and force prediction.
        elif (n_cpus > 1 and gp.per_atom_par and gp.parallel) and not (calculate_efs):
            self.pred_func = predict.predict_on_structure_par_en
        elif not calculate_efs:
            self.pred_func = predict.predict_on_structure_en
        # Energy, force, and stress prediction.
        elif n_cpus > 1 and gp.per_atom_par and gp.parallel:
            self.pred_func = predict.predict_on_structure_efs_par
        else:
            self.pred_func = predict.predict_on_structure_efs

        # set logger
        self.output = Output(output_name, always_flush=True)
        self.output_name = output_name
        self.gp_name = self.output_name + "_gp.json"
        self.checkpt_name = self.output_name + "_checkpt.json"

        self.write_model = write_model

    def run(self):
        """
        Performs an on-the-fly training run.

        If OTF has store_dft_output set, then the specified DFT files will
        be copied with the current date and time prepended in the format
        'Year.Month.Day:Hour:Minute:Second:'.
        """

        optional_dict = {"Restart": self.curr_step}
        self.output.write_header(
            str(self.gp),
            self.dt,
            self.number_of_steps,
            self.structure,
            self.std_tolerance,
            optional_dict,
        )

        counter = 0
        self.start_time = time.time()

        while self.curr_step < self.number_of_steps:
            # run DFT and train initial model if first step and DFT is on
            if (
                (self.curr_step == 0)
                and (self.std_tolerance != 0)
                and (len(self.gp.training_data) == 0)
            ):

                # Are the recorded forces from the GP or DFT in ASE OTF?
                # When DFT is called, ASE energy, forces, and stresses should
                # get updated.
                self.initialize_train()

            # after step 1, try predicting with GP model
            else:
                # compute forces and stds with GP
                self.dft_step = False
                self.compute_properties()

                # get max uncertainty atoms
                std_in_bound, target_atoms = is_std_in_bound(
                    self.std_tolerance,
                    self.gp.force_noise,
                    self.structure,
                    max_atoms_added=self.max_atoms_added,
                    update_style=self.update_style,
                    update_threshold=self.update_threshold,
                )

                if (not std_in_bound) and (
                    self.steps_since_dft > self.min_steps_with_model
                ):
                    # record GP forces
                    self.update_temperature()
                    self.record_state()
                    gp_frcs = deepcopy(self.structure.forces)

                    # run DFT and record forces
                    self.dft_step = True
                    self.steps_since_dft = 0
                    self.run_dft()
                    dft_frcs = deepcopy(self.structure.forces)
                    dft_stress = deepcopy(self.structure.stress)
                    dft_energy = self.structure.potential_energy

                    # run MD step & record the state
                    self.record_state()

                    # compute mae and write to output
                    self.compute_mae(gp_frcs, dft_frcs)

                    # add max uncertainty atoms to training set
                    self.update_gp(
                        target_atoms,
                        dft_frcs,
                        dft_stress=dft_stress,
                        dft_energy=dft_energy,
                    )

            # write gp forces
            if counter >= self.skip and not self.dft_step:
                self.update_temperature()
                self.record_state()
                counter = 0

            counter += 1
            # TODO: Reinstate velocity rescaling.
            self.md_step()  # update positions by Verlet
            self.steps_since_dft += 1
            self.rescale_temperature(self.structure.positions)

            self.curr_step += 1

            if self.write_model == 3:
                self.checkpoint()

        self.output.conclude_run()

        if self.write_model >= 1:
            self.write_gp()
            self.checkpoint()

    def get_structure_from_input(self, prev_pos_init):
        positions, species, cell, masses = self.dft_module.parse_dft_input(
            self.dft_input
        )

        self.structure = struc.Structure(
            cell=cell,
            species=species,
            positions=positions,
            mass_dict=masses,
            prev_positions=prev_pos_init,
            species_labels=species,
        )

    def initialize_train(self):
        # call dft and update positions
        self.run_dft()
        dft_frcs = deepcopy(self.structure.forces)
        dft_stress = deepcopy(self.structure.stress)
        dft_energy = self.structure.potential_energy

        self.update_temperature()
        self.record_state()

        # make initial gp model and predict forces
        self.update_gp(
            self.init_atoms, dft_frcs, dft_stress=dft_stress, dft_energy=dft_energy
        )

    def compute_properties(self):
        """
        In ASE-OTF, it will be replaced by subclass method
        """
        self.gp.check_L_alpha()
        self.pred_func(self.structure, self.gp, self.n_cpus)

    def md_step(self):
        """
        Take an MD step. This updates the positions of the structure.
        """
        md.update_positions(self.dt, self.noa, self.structure)

    def write_gp(self):
        self.gp.write_model(self.gp_name)

    def run_dft(self):
        """Calculates DFT forces on atoms in the current structure.

        If OTF has store_dft_output set, then the specified DFT files will
        be copied with the current date and time prepended in the format
        'Year.Month.Day:Hour:Minute:Second:'.

        Calculates DFT forces on atoms in the current structure."""

        f = logging.getLogger(self.output.basename + "log")
        f.info("\nCalling DFT...\n")

        # calculate DFT forces
        # TODO: Return stress and energy
        forces = self.dft_module.run_dft_par(
            self.dft_input,
            self.structure,
            self.dft_loc,
            n_cpus=self.n_cpus,
            dft_out=self.dft_output,
            npool=self.npool,
            mpi=self.mpi,
            dft_kwargs=self.dft_kwargs,
        )

        self.structure.forces = forces

        # write wall time of DFT calculation
        self.dft_count += 1
        self.output.conclude_dft(self.dft_count, self.start_time)

        # Store DFT outputs in another folder if desired
        # specified in self.store_dft_output
        if self.store_dft_output is not None:
            dest = self.store_dft_output[1]
            target_files = self.store_dft_output[0]
            now = datetime.now()
            dt_string = now.strftime("%Y.%m.%d:%H:%M:%S:")
            if isinstance(target_files, str):
                to_copy = [target_files]
            else:
                to_copy = target_files
            for ofile in to_copy:
                copyfile(ofile, dest + "/" + dt_string + ofile)

    def update_gp(
        self,
        train_atoms: List[int],
        dft_frcs: "ndarray",
        dft_energy: float = None,
        dft_stress: "ndarray" = None,
    ):
        """
        Updates the current GP model.


        Args:
            train_atoms (List[int]): List of atoms whose local environments
                will be added to the training set.
            dft_frcs (np.ndarray): DFT forces on all atoms in the structure.
        """
        self.output.add_atom_info(train_atoms, self.structure.stds)

        if self.force_only:
            dft_energy = None
            dft_stress = None

        # update gp model
        self.gp.update_db(
            self.structure,
            dft_frcs,
            custom_range=train_atoms,
            energy=dft_energy,
            stress=dft_stress,
        )

        self.gp.set_L_alpha()

        # write model
        if (self.dft_count - 1) < self.freeze_hyps:
            self.train_gp()
            if self.write_model == 2:
                self.write_gp()
        if self.write_model == 3:
            self.write_gp()

    def train_gp(self):
        """Optimizes the hyperparameters of the current GP model."""

        self.gp.train(logger_name=self.output.basename + "hyps")

        hyps, labels = self.gp.hyps_and_labels
        if labels is None:
            labels = self.gp.hyp_labels

        self.output.write_hyps(
            labels,
            hyps,
            self.start_time,
            self.gp.likelihood,
            self.gp.likelihood_gradient,
            hyps_mask=self.gp.hyps_mask,
        )

    def compute_mae(self, gp_frcs, dft_frcs):
        mae = np.mean(np.abs(gp_frcs - dft_frcs))
        mac = np.mean(np.abs(dft_frcs))

        f = logging.getLogger(self.output.basename + "log")
        f.info(f"mean absolute error: {mae:.4f} eV/A")
        f.info(f"mean absolute dft component: {mac:.4f} eV/A")

    def rescale_temperature(self, new_pos: "ndarray"):
        """Change the previous positions to update the temperature

        Args:
            new_pos (np.ndarray): Positions of atoms in the next MD frame.
        """
        if self.curr_step in self.rescale_steps:
            rescale_ind = self.rescale_steps.index(self.curr_step)
            temp_fac = self.rescale_temps[rescale_ind] / self.temperature
            vel_fac = np.sqrt(temp_fac)
            self.structure.prev_positions = (
                new_pos - self.velocities * self.dt * vel_fac
            )

    def update_temperature(self):
        """Updates the instantaneous temperatures of the system.

        Args:
            new_pos (np.ndarray): Positions of atoms in the next MD frame.
        """
        KE, temperature, velocities = md.calculate_temperature(
            self.structure, self.dt, self.noa
        )
        self.KE = KE
        self.temperature = temperature
        self.velocities = velocities

    def record_state(self):
        self.output.write_md_config(
            self.dt,
            self.curr_step,
            self.structure,
            self.temperature,
            self.KE,
            self.start_time,
            self.dft_step,
            self.velocities,
        )

    def as_dict(self):
        self.dft_module = self.dft_module.__name__
        out_dict = deepcopy(dict(vars(self)))
        self.dft_module = eval(self.dft_module)

        out_dict["gp"] = self.gp_name
        out_dict["structure"] = self.structure.as_dict()

        for key in ["output", "pred_func"]:
            out_dict.pop(key)

        return out_dict

    @staticmethod
    def from_dict(in_dict):
        if in_dict["write_model"] <= 1:  # TODO: detect GP version
            warnings.warn("The GP model might not be the latest")

        gp_model = gp.GaussianProcess.from_file(in_dict["gp"])
        in_dict["gp"] = gp_model
        in_dict["structure"] = struc.Structure.from_dict(in_dict["structure"])

        if "flare.dft_interface" in in_dict["dft_module"]:
            for dft_name in ["qe", "cp2k", "vasp"]:
                if dft_name in in_dict["dft_module"]:
                    in_dict["force_source"] = dft_name
                    break
        else:  # if force source is a module
            in_dict["force_source"] = eval(in_dict["dft_module"])

        new_otf = OTF(**in_dict)
        new_otf.structure = in_dict["structure"]
        new_otf.dft_count = in_dict["dft_count"]
        new_otf.curr_step = in_dict["curr_step"]
        new_otf.std_tolerance = in_dict["std_tolerance"]
        return new_otf

    def checkpoint(self):
        name = self.checkpt_name
        if ".json" != name[-5:]:
            name += ".json"
        with open(name, "w") as f:
            json.dump(self.as_dict(), f, cls=NumpyEncoder)

    @classmethod
    def from_checkpoint(cls, filename):
        with open(filename, "r") as f:
            otf_model = cls.from_dict(json.loads(f.readline()))

        return otf_model
