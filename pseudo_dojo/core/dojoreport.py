"""
"""
from __future__ import unicode_literals, division, print_function

import sys
import json
import logging
import copy
import numpy as np

from collections import OrderedDict, defaultdict, Iterable
from tabulate import tabulate
from monty.json import MSONable, MontyEncoder
from monty.string import list_strings, is_string
from monty.bisect import find_le
from pymatgen.analysis.eos import EOS
from pymatgen.core.periodic_table import Element
from pymatgen.util.plotting_utils import add_fig_kwargs, get_ax_fig_plt
from pseudo_dojo.refdata.deltafactor import df_database, df_compute


logger = logging.getLogger(__name__)


class DojoEcutResults(object):
    """
    "ecut": "32.0"
    "pawecutdg": "64.0",
    "b0": 0.06400805819081799,
    "b0_GPa": 10.255221080448488,
    "b1": 2.6449207740813594,
    "dfact_meV": 0.2774768889565598,
    "dfactprime_meV": 4.701668998922405,
    "etotals": []
    "num_sites": 4,
    "v0": 17.264380250637252,
    "volumes": [],

    ecuts = dfres.get_ecuts()
    pawecutdgs = dfres.get_pawecutdgs()
    b0_values = dfres.get_values("b0")
    dfres.insert(data_dict)
    data = dfres.get_data_for_ecut(ecut)
    dfres.plot_ecut_convergence()
    """
    def __init__(self, dict_list=None, metadata=None):
        self.dict_list = [] if dict_list is None else dict_list
        self.metadata = {} if metadata is None else metadata

    @staticmethod
    def class_from_name(name):
        for cls in DojoEcutResults.__subclasses__():
            if cls.name == name: return cls
        raise ValueError("Cannot find class associated to name: %s" % name)

    @staticmethod
    def all_names_and_classes():
        return [(cls.name, cls) for cls in DojoEcutResults.__subclasses__()]

    def insert(self, data):
        """
        Insert new data so that the list is still ordered with increasing ecut
        If an ecut is already stored, we replace the old entry.
        """
        # Handle first insertion
        if not self.dict_list:
            self.dict_list.append(data)
            return

        prev_ecuts = self.get_ecuts()
        new_ecut = float(data["ecut"])

        # Find rightmost value less than or equal to x.
        already_in = False
        if new_ecut < prev_ecuts[0]:
            i = 0
        elif new_ecut > prev_ecuts[-1]:
            i = len(prev_ecuts)
        else:
            i = find_le(prev_ecuts, new_ecut)
            # Handle possible dupe.
            already_in = prev_ecuts[i] == new_ecut
            if already_in:
                self.dict_list.pop(i)
            else:
                i += 1

        self.dict_list.insert(i, data)

    def get_data_for_ecut(self, ecut):
        """Return the results for the given ecut"""
        for data in self:
            if abs(float(data["ecut"]) - float(ecut)) < 0.001: return data
        raise ValueError("Cannot find ecut = %s" % ecut)

    @staticmethod
    def from_dict(d):
        cls = DojoEcutResults.class_from_name(d["name"])
        return cls(dict_list=d["dict_list"], metadata=d["metadata"])

    #@pmg_serialize
    def as_dict(self):
        return dict(name=self.name, dict_list=self.dict_list, metadata=self.metadata)

    def __len__(self):
        return self.dict_list.__len__()

    def __iter__(self):
        return self.dict_list.__iter__()

    def __str__(self):
        return str(self.as_dict())

    def get_ecuts(self):
        return [d.get("ecut") for d in self.dict_list]

    def get_pawecutdgs(self):
        return [d.get("pawecutdg") for d in self.dict_list if "pawecutdg" in d]

    def get_values(self, vname):
        return [d.get(vname) for d in self.dict_list]

    #def get_dataframe(self):


class DeltaFactorResults(DojoEcutResults):
    name = "deltafactor"


class GbrvFccResults(DojoEcutResults):
    name = "gbrv_fcc"


class GbrvBccResults(DojoEcutResults):
    name = "gbrv_bcc"


class PhononResults(DojoEcutResults):
    name = "phonon"


class EbandsResults(DojoEcutResults):
    name = "ghosts"


class DojoReportError(Exception):
    """Exception raised by DoJoReport."""


class DojoReport(dict):
    """
    Dict-like object with the validation results.

    {
    "version": "1.0"
    "symbol": "H",
    "pseudo_type": "NC",
    "md5": "13198abb7506a840b7d46ef46b54d789",
    "ppgen_hints": {
        "low": {"ecut": 30.0,  "pawecutdg": 30.0},
        "normal": {"ecut": 34.0, "pawecutdg": 34.0},
        "high": {"ecut": 39.0, "pawecutdg": 39.0}
    },
    "hints": {
        "low": {"ecut": 30.0,  "pawecutdg": 30.0},
        "normal": {"ecut": 34.0, "pawecutdg": 34.0},
        "high": {"ecut": 39.0, "pawecutdg": 39.0},
    },
    "ecuts": [29.0, 31.0, 33.0],
    "deltafactor": {}
    "gbrv_bcc": {},
    "gbrv_fcc": {},
    "ghosts": []
    "phonons": []
    }
    """
    # List of dojo_trials
    # Remember to update the list if you add a new test to the DOJO_REPORT
    ALL_TRIALS = (
        "deltafactor",
        "gbrv_bcc",
        "gbrv_fcc",
        "phonon",
        "phwoa",
        "ghosts",
    )

    _TRIALS2KEY = {
        "deltafactor": "dfact_meV",
        "gbrv_bcc": "a0_rel_err",
        "gbrv_fcc": "a0_rel_err",
        "phwoa": "all",
        "phonon": "all",
        "ghosts": "all",
    }

    # We use three different level of accuracy.
    ALL_ACCURACIES = ("low", "normal", "high")

    # Tolerances on the deltafactor prime (in eV) used for the hints.
    ATOLS = (0.5, 0.1, 0.02)
    # For noble gasses:
    #ATOLS = (1.0, 0.2, 0.04)

    # Version of the DojoReport.
    LAST_VERSION = "1.0"

    Error = DojoReportError

    @classmethod
    def from_file(cls, filepath):
        """Read the DojoReport from file."""
        with open(filepath, "rt") as fh:
            return cls(**json.load(fh))

    @classmethod
    def empty_from_pseudo(cls, pseudo, hints, devel=False):
        """
        Initialize an empty DojoReport from the pseudo and an initial guess for
        the cutoff energy in Hartree

        Args:
            pseudo: Pseudo object.
            hints: Initial hints on the cutoff energy provided by the pp generator.
                Dictionary [accuracy][ecut]
        """
        # Build initial list of cutoff energies for tests.
        #dense_right = np.arange(ppgen_ecut, ppgen_ecut + 6*2, step=2)
        #dense_left = np.arange(max(ppgen_ecut-6, 2), ppgen_ecut, step=2)
        #coarse_high = np.arange(ppgen_ecut + 15, ppgen_ecut + 35, step=5)

        new = cls()

        estart = hints["high"]["ecut"]
        dense_right = np.linspace(estart - 10, estart + 10, num=11)
        ecuts = list(dense_right) + [dense_right[-1] + 8, dense_right[-1] + 10,],

        # devel is for tuning the pseudo, only two cutoffs
        # development run: few, relatively high ecut calculations
        if devel: ecuts = [estart, estart + 2]

        if pseudo.isnc:
            pseudo_type = "NC"
        elif pseudo.ispaw:
            pseudo_type = "PAW"
        else:
            raise TypeValue("Neither NC nor PAW pseudo!")

        new.update(
            version=cls.LAST_VERSION,
            symbol=pseudo.symbol,
            pseudo_type=pseudo_type,
            xc=pseudo.xc.as_dict(),
            md5=pseudo.compute_md5(),
            ppgen_hints=hints,
            ecuts=ecuts,
        )

        return new

    def to_dict(self):
        d = {k: v for k, v in self.items()}
        for name in DojoEcutResults.all_names():
            if name not in d: continue
            d[name] = d[name].to_dict()
        return d

    def from_dict(cls, d):
        # Preventive copy becayse we are gonna change the input dict.
        d = copy.deepcopy(d)

        new = cls()

        # Create instances of DojoEcutResults and add them to new.
        results = []
        for name, res_cls in DojoEcutResults.all_names_and_classes():
            if name not in d: continue
            res = res_cls.from_dict(d.pop(name))
            results.append(res)
            new[name] = res

        # Inglobate the rest
        new.update(d)
        return new

    @classmethod
    def from_hints(cls, ppgen_ecut, symbol):
        """
        Initialize an empty DojoReport from the initial guesses for
        the cutoff energy in Hartree

        Args:
            ppgen_ecut: tuple(3) cutoff energies for the 3 accuracy levels.
            symbol: Chemical symbol.
        """
        dense_right = np.arange(ppgen_ecut, ppgen_ecut + 6*2, step=2)
        dense_left = np.arange(max(ppgen_ecut-6, 2), ppgen_ecut, step=2)
        coarse_high = np.arange(ppgen_ecut + 15, ppgen_ecut + 35, step=5)

        ecuts = list(dense_left) + list(dense_right) + list(coarse_high)
        return cls(ecuts=ecuts, symbol=symbol)

    def __init__(self, *args, **kwargs):
        super(DojoReport, self).__init__(*args, **kwargs)

        try:
            for trial in self.ALL_TRIALS:
                # Convert ecut to float and build an OrderedDict (results are indexed by ecut in ascending order)
                try:
                    d = self[trial]
                except KeyError:
                    continue
                ecuts_keys = sorted([(float(k), k) for k in d], key=lambda t: t[0])
                self[trial] = OrderedDict([(t[0], d[t[1]]) for t in ecuts_keys])

        except ValueError:
            raise self.Error('Error while initializing the dojo report')

        if "version" not in self:
            self["version"] = self.LAST_VERSION

    @property
    def exceptions(self):
        """List of exceptions."""
        return self.get("_exceptions", [])

    def push_exception(self, string):
        """Register an exception."""
        if "_exceptions" not in self:
            self["_exceptions"] = []
        self["_exceptions"].append(string)

    def remove_exceptions(self):
        """Remove the exception entry from the dictionary."""
        return self["_exceptions"].pop()

    def __str__(self):
        """String representation."""
        return(json.dumps(self, indent=-1))

    def deepcopy(self):
        """Deepcopy of the object."""
        return copy.deepcopy(self)

    def json_write(self, filepath):
        """Write data to file."""
        with open(filepath, "wt") as fh:
            #json.dump(self, fh, sort_keys=True, cls=MontyEncoder)
            json.dump(self, fh, indent=-1, sort_keys=True, cls=MontyEncoder)

    @property
    def symbol(self):
        """Chemical symbol."""
        return self["symbol"]

    @property
    def element(self):
        """Element object."""
        return Element(self.symbol)

    @property
    def ecuts(self):
        """Numpy array with the list of ecuts that should be present in the dojo_trial sub-dicts"""
        return self["ecuts"]

    @property
    def trials(self):
        """Set of strings with the trials present in the report."""
        return set(list(self.keys())).intersection(self.ALL_TRIALS)

    def has_trial(self, dojo_trial, ecut=None):
        """
        True if the dojo_report contains dojo_trial with the given ecut.
        If ecut is None, we test if dojo_trial is present.
        """
        #if dojo_trial not in self.ALL_TRIALS:
        #    raise ValueError("dojo_trial `%s` is not a registered DOJO TRIAL" % dojo_trial)

        if dojo_trial not in self: return False
        if ecut is None: return dojo_trial in self

        # input ecut could be either float or string
        if ecut in self[dojo_trial]: return True
        ecut_str = self._ecut2key(ecut)
        if ecut_str in self[dojo_trial]: return True
        return False

    def add_ecuts(self, new_ecuts):
        """Add a list of new ecut values."""
        # Be careful with the format here! it should be %.1f
        # Select the list of ecuts reported in the DOJO section.
        prev_ecuts = self["ecuts"]

        for i in range(len(prev_ecuts)-1):
            if prev_ecuts[i] >= prev_ecuts[i+1]:
                raise self.Error("Ecut list is not ordered:\n %s" % prev_ecuts)

        if not isinstance(new_ecuts, Iterable): new_ecuts = [new_ecuts]
        for e in new_ecuts:
            # Find rightmost value less than or equal to x.
            if e < prev_ecuts[0]:
                i = 0
            elif e > prev_ecuts[-1]:
                i = len(prev_ecuts)
            else:
                i = find_le(prev_ecuts, e)
                assert prev_ecuts[i] != e
                i += 1

            prev_ecuts.insert(i, e)

    @property
    def has_hints(self):
        """True if hints on the cutoff energy are present."""
        return "hints" in self

    def add_hints(self, hints):
        """Add hints on the cutoff energy."""
        hints_dict = {
           "low": {'ecut': hints[0]},
           "normal": {'ecut': hints[1]},
           "high": {'ecut': hints[2]}
                     }
        self["hints"] = hints_dict

    def ipw_validate(self):
        """
        Return an ipython widget with controllers to validate the pseudo.
        """
        import ipywidgets as ipw

        low_ecut = ipw.FloatText(description='Low ecut:')
        normal_ecut = ipw.FloatText(description='Normal ecut:')
        high_ecut = ipw.FloatText(description='High ecut:')
        validated_by = ipw.Text(description="Validated by:")
        ok_button = ipw.Button(description="Validate")

        def on_button_clicked(b):
            """Callback called to validate the dojo report."""
            print(low_ecut.value, normal_ecut.value, high_ecut.value)
            if not low_ecut.value <= normal_ecut.value <= high_ecut.value:
                raise ValueError("not low_ecut.value <= normal_ecut.value <= high_ecut.value")
            if not validated_by.value:
                raise ValueError("validated_by field must be filled")
            #if "validation" in self and not force_new_validation:
            #    raise ValueError("DojoReport is already validated. Use force_new_validation")

            #from time import gmtime, strftime
            #self['validation'] = {
            #    'validated_by': validated_by.value,
            #    'validated_on': strftime("%Y-%m-%d %H:%M:%S", gmtime())
            #}
            #self.json_write(p.djrepo_path)

            # TODO: Print convergence of df, gbrv ...
            #df_last_ecut = sorted((self["deltafactor"].keys())[-1]
            #df_last = self["delfactor"][df_last_ecut]["dfact_meV"]
            #dfprime_last = self["delfactor"][df_last_ecut]["dfactprime_meV"]

        ok_button.on_click(on_button_clicked)
        return ipw.Box(children=[low_ecut, normal_ecut, high_ecut, validated_by, ok_button])

    @staticmethod
    def _ecut2key(ecut):
        """Convert ecut to a valid key. ecut can be either a string or a float."""
        if is_string(ecut):
            # Validate string
            i = ecut.index(".")
            if len(ecut[i+1:]) != 1:
                raise ValueError("string %s must have one digit")
            return ecut

        else:
            # Assume float
            return "%.1f" % ecut

    def add_entry(self, dojo_trial, ecut, entry, overwrite=False):
        """
        Add an entry computed with the given ecut to the sub-dictionary associated to dojo_trial.

        Args:
            dojo_trial: String defining the dojo trial.
            ecut: Cutoff energy in Hartree
            entry: Dictionary with data.
            overwrite: By default, this method raises ValueError if this entry is already filled.
        """
        #if dojo_trial not in self.ALL_TRIALS:
        #    raise ValueError("%s is not a registered trial")

        if dojo_trial not in self: self[dojo_trial] = {}
        section = self[dojo_trial]

        key = self._ecut2key(ecut)
        if key in section and not overwrite:
            raise self.Error("Cannot overwrite key %s in dojo_trial %s" % (key, dojo_trial))

        # Add entry to section.
        section[key] = entry

    def find_missing_entries(self):
        """
        Check the DojoReport.
        This function tests if each trial contains an ecut entry.
        Return a dictionary {trial_name: [list_of_missing_ecuts]}
        mapping the name of the Dojo trials to the list of ecut values that are missing
        """
        d = {}

        for trial in self.ALL_TRIALS:
            data = self.get(trial, None)
            if data is None:
                # Gbrv results do not contain noble gases so ignore the error
                if "gbrv" in trial and self.element.is_noble_gas:
                    assert data is None
                    continue
                d[trial] = self.ecuts

            else:
                computed_ecuts = list(data.keys())
                for e in self.ecuts:
                    if e not in computed_ecuts:
                        if trial not in d: d[trial] = []
                        d[trial].append(e)

        if not d:
            assert len(computed_ecuts) == len(self.ecuts)

        return d

    def get_ecut_dfactprime(self):
        """Return numpy arrays wit ecut list and the corresponding dfactprime values."""
        data = self["deltafactor"]
        ecuts, values= data.keys(), []
        values = np.array([data[e]["dfactprime_meV"] for e in ecuts])
        return np.array(ecuts), values

    def get_last_df_dfp(self):
        """
        Return the last value i.e. the best estimate of deltafactor and deltafactor_prime
        None, None if "deltafactor" is not present.
        """
        try:
            data = self["deltafactor"]
        except KeyError:
            return None, None

        ecuts = list(data.keys())
        dfact_meV = [data[e]["dfact_meV"] for e in ecuts][-1]
        dfp = [data[e]["dfactprime_meV"] for e in ecuts][-1]

        return dfact_meV, dfp

    def compute_hints(self):
        ecuts, dfacts = self.get_ecut_dfactprime()
        abs_diffs = np.abs((dfacts - dfacts[-1]))
        #print(list(zip(ecuts, dfacts)), abs_diffs)

        hints = 3 * [None]
        for ecut, adiff in zip(ecuts, abs_diffs):
            for i in range(3):
                if adiff <= self.ATOLS[i] and hints[i] is None:
                    hints[i] = ecut
                if adiff > self.ATOLS[i]:
                    hints[i] = None
        return hints

    def check(self, check_trials=None):
        """
        Check the dojo report for inconsistencies.
        Return a string with the errors found in the DOJO_REPORT.

        Args:
            check_trials: string or list of strings selecting the trials to be tested.
                If None, all trials are analyzed.
        """
        check_trials = self.ALL_TRIALS if check_trials is None else list_strings(check_trials)
        errors = []
        app = errors.append

        for k in ("version", "ppgen_hints", "md5"):
            if k not in self: app("%s is missing" % k)

        # Check if we have computed each trial for the full set of ecuts in global_ecuts
        global_ecuts = self.ecuts

        # TODO: report should contain XC
        missing = defaultdict(list)
        for trial in check_trials:
            # Gbrv results do not contain noble gases, Hg and Po so ignore the error
            if "gbrv" in trial and (self.element.is_noble_gas or self.symbol in ("Hg", "Po")):
                continue

            for ecut in global_ecuts:
                if not self.has_trial(trial, ecut=ecut):
                    missing[trial].append(ecut)

        if missing:
            app("%s: the following ecut values are missing:" % self.symbol)
            for trial, ecuts in missing.items():
                app("    %s: %s" % (trial, ecuts))

        for trial in check_trials:
            if not self.has_trial(trial): continue
            for ecut in self[trial]:
                if ecut not in global_ecuts:
                    app("%s: ecut %s is not in the global list" % (trial, ecut))

        return "\n".join(errors)

    def convert(self, new_version):
        """
        Convert to new_version.
        Return new DojoReport object.
        """
        if self["version"] == new_version: return self
        assert new_version == "2.0"

        return new

    @add_fig_kwargs
    def plot_etotal_vs_ecut(self, ax=None, inv_ecut=False, **kwargs):
        """
        plot the convergence of the total energy as function of the energy cutoff ecut

        Args:
            ax: matplotlib Axes, if ax is None a new figure is created.

        Returns:
            `matplotlib` figure or None if the deltafactor test is not present
        """
        trial = "deltafactor"
        if trial not in self:
            print("dojo report does not contain trial:", trial)
            return None

        # Extract the total energy of the AE relaxed structure (4).
        d = OrderedDict([(ecut, data["etotals"][4]) for ecut, data in self["deltafactor"].items()])

        # Ecut mesh in Ha
        ecuts = np.array(list(d.keys()))
        ecut_min, ecut_max = np.min(ecuts), np.max(ecuts)

        # Energies per atom in meV and difference wrt 'converged' value
        num_sites = [v["num_sites"] for v in self["deltafactor"].values()][0]
        etotals_mev = np.array([d[e] for e in ecuts]) * 1000  / num_sites
        ediffs = etotals_mev - etotals_mev[-1]

        ax, fig, plt = get_ax_fig_plt(ax)
        #ax.yaxis.set_view_interval(-5, 5)

        lines, legends = [], []

        xs = 1/ecuts if inv_ecut else ecuts
        ys = etotals_mev if inv_ecut else ediffs

        line, = ax.plot(xs, ys, "-o", color="blue") #, linewidth=3.0, markersize=15)
        lines.append(line)

        label = kwargs.pop("label", None)
        if label is not None: ax.legend(lines, [label], loc='best', shadow=True)

        high_hint = self["ppgen_hints"]["high"]["ecut"]
        #ax.vlines(high_hint, min(ediffs), max(ediffs))
        #ax.vlines(high_hint, 0.5, 1.5)
        #ax.scatter([high_hint], [1.0], s=20) #, c='b', marker='o', cmap=None, norm=None)
        #ax.arrow(high_hint, 1, 0, 0.2, head_width=0.05, head_length=0.1, fc='k', ec='k',head_starts_at_zero=False)

        #ax.hlines(5, ecut_min, ecut_max, label="5.0")
        #ax.hlines(1, ecut_min, ecut_max, label="1.0")
        #ax.hlines(0.5, ecut_min, ecut_max, label="0.2")

        # Set xticks and labels.
        ax.grid(True)
        ax.set_xlabel("Ecut [Ha]")
        ax.set_xticks(xs)
        ax.set_ylabel("Delta Etotal/natom [meV]")
        #ax.set_xlim(0, max(xs))

        # Use logscale if possible.
        if all(ediffs[:-1] > 0):
            ax.set_yscale("log")
            ax.set_xlim(xs[0]-1, xs[-2]+1)

        return fig

    @add_fig_kwargs
    def plot_deltafactor_eos(self, ax=None, **kwargs):
        """
        plot the EOS computed with the deltafactor setup.

        Args:
            ax: matplotlib :class:`Axes` or None if a new figure should be created.

        ================  ==============================================================
        kwargs            Meaning
        ================  ==============================================================
        cmap              Color map. default `jet`
        ================  ==============================================================

        Returns:
            `matplotlib` figure or None if the deltafactor test is not present
        """
        trial = "deltafactor"
        if trial not in self:
            print("dojo report does not contain trial:", trial)
            return None

        ax, fig, plt = get_ax_fig_plt(ax)
        cmap = kwargs.pop("cmap", plt.get_cmap("jet"))

        ecuts = list(self[trial].keys())
        num_ecuts = len(ecuts)

        for i, ecut in enumerate(ecuts):
            d = self[trial][ecut]
            num_sites, volumes, etotals = d["num_sites"], np.array(d["volumes"]), np.array(d["etotals"])

            # Use same fit as the one employed for the deltafactor.
            eos_fit = EOS.DeltaFactor().fit(volumes/num_sites, etotals/num_sites)

            label = "ecut %.1f" % ecut if i % 2 == 0 else ""
            label = "ecut %.1f" % ecut
            eos_fit.plot(ax=ax, text=False, label=label, color=cmap(i/num_ecuts, alpha=1), show=False)

        return fig

    @add_fig_kwargs
    def plot_deltafactor_convergence(self, xc, code="WIEN2k", what=None, ax_list=None, **kwargs):
        """
        plot the convergence of the deltafactor parameters wrt ecut.

        Args:
            xc=String or XcFunc object specifying the XC functional. E.g "PBE" or XcFunc.from_name("PBE"
            code: Reference code
            ax_list: List of matplotlib Axes, if ax_list is None a new figure is created

        Returns:
            `matplotlib` figure or None if the deltafactor test is not present
        """
        trial = "deltafactor"
        if trial not in self:
            print("dojo report does not contain trial:", trial)
            return None

        all = ["dfact_meV", "dfactprime_meV", "v0", "b0_GPa", "b1"]
        if what is None:
            keys = all
        else:
            what = list_strings(what)
            if what[0].startswith("-"):
                # Exclude keys
                what = [w[1:] for w in what]
                keys = [k for k in all if k not in what]
            else:
                keys = what

        # Get reference entry
        reference = df_database(xc=xc).get_entry(symbol=self.symbol, code=code)
        print("Reference data:", reference)

        d = self["deltafactor"]
        ecuts = list(d.keys())

        import matplotlib.pyplot as plt
        if ax_list is None:
            fig, ax_list = plt.subplots(nrows=len(keys), ncols=1, sharex=True, squeeze=False)
            ax_list = ax_list.ravel()
        else:
            fig = plt.gcf()

        if len(keys) != len(ax_list):
            raise ValueError("len(keys)=%s != len(ax_list)=%s" %  (len(keys), len(ax_list)))

        for i, (ax, key) in enumerate(zip(ax_list, keys)):
            values = np.array([float(d[ecut][key]) for ecut in ecuts])
            refval = getattr(reference, key)

            # Plot difference pseudo - ref.
            ax.plot(ecuts, values - refval, "o-")

            ax.grid(True)
            ax.set_ylabel("$\Delta$" + key)
            if i == len(keys) - 1: ax.set_xlabel("Ecut [Ha]")

            if key == "dfactprime_meV":
                # Add horizontal lines (used to find hints for ecut).
                last = values[-1]
                xmin, xmax = min(ecuts), max(ecuts)
                for pad, color in zip(self.ATOLS, ("blue", "red", "violet")):
                    ax.hlines(y=last + pad, xmin=xmin, xmax=xmax, colors=color, linewidth=1, linestyles='dashed')
                    ax.hlines(y=last - pad, xmin=xmin, xmax=xmax, colors=color, linewidth=1, linestyles='dashed')

                # Set proper limits so that we focus on the relevant region.
                ax.set_ylim(last - 1.1*self.ATOLS[0], last + 1.1*self.ATOLS[0])

        return fig

    @add_fig_kwargs
    def plot_gbrv_eos(self, struct_type, ax=None, **kwargs):
        """
        Uses Matplotlib to plot the EOS computed with the GBRV setup

        Args:
            ax: matplotlib :class:`Axes` or None if a new figure should be created.

        ================  ==============================================================
        kwargs            Meaning
        ================  ==============================================================
        cmap              Color map. default `jet`
        ================  ==============================================================

        Returns:
            `matplotlib` figure or None if the GBRV test is not present
        """
        trial = "gbrv_" + struct_type
        # Handle missing entries: noble gases, Hg ...
        if trial not in self:
            print("dojo report does not contain trial:", trial)
            return None

        ax, fig, plt = get_ax_fig_plt(ax)
        cmap = kwargs.pop("cmap", plt.get_cmap("jet"))

        ecuts = list(self[trial].keys())
        num_ecuts = len(ecuts)

        for i, ecut in enumerate(ecuts):
            d = self[trial][ecut]
            volumes, etotals = np.array(d["volumes"]), np.array(d["etotals"])

            eos_fit = EOS.Quadratic().fit(volumes, etotals)
            label = "ecut %.1f" % ecut if i % 2 == 0 else ""
            label = "ecut %.1f" % ecut
            eos_fit.plot(ax=ax, text=False, label=label, color=cmap(i/num_ecuts, alpha=1), show=False)

        return fig

    @add_fig_kwargs
    def plot_gbrv_convergence(self, ax_list=None, **kwargs):
        """
        Uses Matplotlib to plot the convergence of the GBRV parameters wrt ecut.

        Args:
            ax_list: List of matplotlib Axes, if ax_list is None a new figure is created

        Returns:
            `matplotlib` figure. None if the GBRV test is not present.
        """
        import matplotlib.pyplot as plt
        stypes = ("fcc", "bcc")

        for stype in stypes:
            trial = "gbrv_" + stype
            if trial not in self:
                print("dojo report does not contain trial:", trial)
                return None

        if ax_list is None:
            fig, ax_list = plt.subplots(nrows=len(stypes), ncols=1, sharex=True, squeeze=False)
            ax_list = ax_list.ravel()
        else:
            fig = plt.gcf()

        if len(stypes) != len(ax_list):
            raise ValueError("len(stypes)=%s != len(ax_list)=%s" %  (len(stypes), len(ax_list)))

        for i, (ax, stype) in enumerate(zip(ax_list, stypes)):
            trial = "gbrv_" + stype
            d = self[trial]
            ecuts = list(d.keys())
            values = np.array([float(d[ecut]["a0_rel_err"]) for ecut in ecuts])

            ax.grid(True)
            ax.set_ylabel("$\Delta$" + trial + "a0_rel_err")

            # Plot difference pseudo - ref.
            ax.plot(ecuts, values, "bo-")
            #ax.hlines(y=0.0, xmin=min(ecuts), xmax=max(ecuts), color="red")
            if i == len(ax_list) - 1: ax.set_xlabel("Ecut [Ha]")

        return fig

    @add_fig_kwargs
    def plot_phonon_convergence(self, ax_list=None, **kwargs):
        """
        Plot the convergence of the phonon modes wrt ecut.

        Args:
            ax_list: List of matplotlib Axes, if ax_list is None a new figure is created

        Returns:
            `matplotlib` figure. None if the GBRV test is not present.
        """
        trial = "phonon"
        if trial not in self:
            print("dojo report does not contain trial:", trial)
            return None

        d = self[trial]
        ecuts = list(d.keys())

        l = [(ecut, float(ecut)) for ecut in ecuts]
        s = sorted(l, key=lambda t: t[1])
        max_ecut = s[-1][0]
        s_ecuts = [ecut[0] for ecut in s]

        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(nrows=2, sharex=True)
        #ax_list, fig, plt = get_axarray_fig_plt(ax_list, nrows=len(keys), ncols=1, sharex=True, squeeze=False)

        fmin, fmax = np.inf, -np.inf
        for i, v in enumerate(d[ecuts[0]]):
            values1 = np.array([float(d[ecut][i]) for ecut in s_ecuts])
            fmin = min(fmin, values1.min())
            fmax = max(fmax, values1.max())

            ax[0].plot(s_ecuts, values1, "o-")
            ax[0].grid(True)
            ax[0].set_ylabel("phonon modes [meV] (asr==2)")
            ax[0].set_xlabel("Ecut [Ha]")

            values2 = np.array([float(d[ecut][i]) - float(d[max_ecut][i]) for ecut in s_ecuts])

            ax[1].plot(s_ecuts, values2, "o-")
            ax[1].grid(True)
            ax[1].set_ylabel("w - w(ecut_max) [meV]")
            ax[1].set_xlabel("Ecut [Ha]")

        # Adjust limits.
        fmin -= 10
        fmax += 10
        ax[0].set_ylim(fmin, fmax)

        return fig

    @add_fig_kwargs
    def plot_ebands(self, ecut=None, **kwargs):
        """
        Plot electronic band structure.

        ================  =============================
        kwargs            Meaning
        ================  =============================
        width             Gaussian broadening in eV
        step              Step of the DOS mesh in eV
        ================  =============================

        Returns:
            `matplotlib` figure. None if the ebands test is not present.
        """
        trial = "ghosts"
        if trial not in self:
            print("dojo report does not contain trial:", trial)
            return None

        if ecut is None: ecut = list(self[''].keys())[0]
        d = self[trial]["%.1f" % ecut][ebands]
        from abipy.electrons.ebands import ElectronBands
        ebands = ElectronBands.from_dict(d)
        edos = ebands.get_edos(width=kwargs.pop("width", 0.05), step=kwargs.pop("step", 0.02))

        return ebands.plot_with_edos(edos, **kwargs)

######################
## Pandas DataFrame ##
######################

from pandas import DataFrame

class DojoDataFrame(DataFrame):
    """
    Extends pandas DataFrame adding helper functions.
    """

    # The frame has its own list so that one can easily change the
    # entries that should be analyzed by modifying this attributes.
    ALL_ACCURACIES = DojoReport.ALL_ACCURACIES
    ALL_TRIALS = DojoReport.ALL_TRIALS

    _TRIALS2KEY = {
        "ecut": "ecut",
        "deltafactor": "dfact_meV",
        "gbrv_bcc": "gbrv_bcc_a0_rel_err",
        "gbrv_fcc": "gbrv_fcc_a0_rel_err",
        "phonon": "all",
        "phwoa": "all",
        "ghosts": "all"
    }

    _TRIALS2YLABEL = {
        "ecut": "Ecut [Ha]",
        "deltafactor": "$\Delta$-factor [meV]",
        "gbrv_bcc": "BCC $\Delta a_0$ (%)",
        "gbrv_fcc": "FCC $\Delta a_0$ (%)",
        "phonon": "Phonons with ASR",
        "phwoa": "Phonons without ASR",
        "ghosts": "Electronic band structure"
    }

    ACC2PLTOPTS = dict(
        low=dict(color="red"),
        normal=dict(color="blue"),
        high=dict(color="green"),
    )

    for v in ACC2PLTOPTS.values():
        v.update(linewidth=2, linestyle='dashed', marker='o', markersize=8)

    @classmethod
    def from_pseudos(cls, pseudos):
        """
        Buid a pandas :class:`DataFrame` with the most important parameters extracted from the
        `DOJO_REPORT` section of each pseudo in the table.

        Returns:
            frame, errors

            where frame is the pandas :class:`DataFrame` and errors is a list of errors
            encountered while trying to read the `DOJO_REPORT` from the pseudopotential file.
        """
        accuracies = ["low", "normal", "high"]

        trial2keys = {
            "deltafactor": ["dfact_meV", "dfactprime_meV"] + ["v0", "b0_GPa", "b1"],
            "gbrv_bcc": ["a0_rel_err"],
            "gbrv_fcc": ["a0_rel_err"],
            "phonon": "all",
            #"phwoa": "all"
        }

        rows, names, errors = [], [], []

        for p in pseudos:
            if not p.has_dojo_report:
                print("Cannot find dojo_report in ", p.basename)
                continue

            report = p.dojo_report
            if "version" not in report:
                print("ignoring old report in ", p.basename)
                continue

            d = {"symbol": p.symbol, "Z": p.Z, "filepath": p.filepath}
            names.append(p.basename)

            ecut_acc = {}

            # read hints
            for acc in accuracies:
                try:
                    d.update({acc + "_ecut_hint": report['hints'][acc]['ecut']})
                    ecut_acc[acc] = report['hints'][acc]['ecut']
                except KeyError:
                    # using -1 for non existing values facilitates plotting
                    d.update({acc + "_ecut_hint": -1.0 })
                    ecut_acc[acc] = -1

            for acc in accuracies:
                d[acc + "_ecut"] = ecut_acc[acc]

            try:
                for trial, keys in trial2keys.items():
                    data = report.get(trial, None)

                    if data is None: continue

                    # if the current trial has an entry for this ecut change nothing, else we take the
                    # smallest, the middle and the highest ecut available for this trials
                    # precausion, normally either there are hints or not. in the second case they are all set to -1
                    ecut_acc_trial = dict(
                        low=sorted(data.keys())[0],
                        normal=sorted(data.keys())[int(len(data.keys())/2)],
                        high=sorted(data.keys())[-1],
                    )

                    for acc in accuracies:
                        d[acc + "_ecut"] = ecut_acc[acc]

                    for acc in accuracies:
                        ecut = ecut_acc[acc] if ecut_acc[acc] in data.keys() else ecut_acc_trial[acc]
                        #store the actuall ecut for this trial
                        d.update({acc + "_ecut_" + trial: ecut})
                        if keys is 'all':
                            ecuts = data
                            d.update({acc + "_" + trial: data[ecut]})
                        else:
                            if trial.startswith("gbrv"):
                                d.update({acc + "_" + trial + "_" + k: float(data[ecut][k]) for k in keys})
                            else:
                                d.update({acc + "_" + k: float(data[ecut][k]) for k in keys})

            except Exception as exc:
                logger.warning("%s raised %s" % (p.basename, exc))
                errors.append((p.basename, str(exc)))

            rows.append(d)

        # Build sub-class of pandas.DataFrame
        return cls(rows, index=names), errors

    def tabulate(self, columns=None, stream=sys.stdout):
        if columns is None:
            accuracies = self.ALL_ACCURACIES
            columns = [acc + "_dfact_meV" for acc in accuracies]
            columns += [acc + "_ecut" for acc in accuracies]
            columns += [acc + "_gbrv_fcc_a0_rel_err" for acc in accuracies]
            columns += [acc + "_gbrv_bcc_a0_rel_err" for acc in accuracies]

        #return self[columns].to_html()
        tablefmt = "grid"
        floatfmt=".2f"
        stream.write(tabulate(self[columns], headers="keys", tablefmt=tablefmt, floatfmt=floatfmt))

    def get_accuracy(self, accuracy):
        columns = [c for c in self if c.startswith(accuracy)]
        return self.__class__(data=self[columns])

    def get_trials(self, accuracies="all"):
        accuracies = self.ALL_ACCURACIES if accuracies == "all" else list_strings(accuracies)

        columns = [acc + "_dfact_meV" for acc in accuracies]
        columns += [acc + "_ecut" for acc in accuracies]
        columns += [acc + "_gbrv_fcc_a0_rel_err" for acc in accuracies]
        columns += [acc + "_gbrv_bcc_a0_rel_err" for acc in accuracies]

        return self.__class__(data=self[columns])

    def select_rows(self, rows):
        if not isinstance(rows, (list, tuple)): rows = [rows]

        data = []
        for index, entry in self.iterrows():
            element = Element.from_Z(entry.Z)
            if element.row in rows:
                data.append(entry)

        return self.__class__(data=data)

    def select_family(self, family):
        data = []
        for index, entry in self.iterrows():
            element = Element.from_Z(entry.Z)
            # e.g element.is_alkaline
            if getattr(element, "is_" + family): data.append(entry)
        return self.__class__(data=data)

    @add_fig_kwargs
    def plot_hist(self, what="dfact_meV", bins=400, **kwargs):
        import matplotlib.pyplot as plt
        fig, ax_list = plt.subplots(nrows=len(self.ALL_ACCURACIES), ncols=1, sharex=True, sharey=False, squeeze=True)

        for acc, ax in zip(self.ALL_ACCURACIES, ax_list):
            col = acc + "_" + what
            #print(col)
            #self[col].hist(ax=ax, bins=bins, label=col)
            self[col].plot(ax=ax, kind="bar", label=col)

        return fig

    @add_fig_kwargs
    def plot_trials(self, trials="all", accuracies="all", **kwargs):
        import matplotlib.pyplot as plt
        trials = self.ALL_TRIALS if trials == "all" else list_strings(trials)
        accuracies = self.ALL_ACCURACIES if accuracies == "all" else list_strings(accuracies)

        fig, ax_list = plt.subplots(nrows=len(trials), ncols=1, sharex=True, sharey=False, squeeze=True)

        # See also http://matplotlib.org/examples/pylab_examples/barchart_demo.html
        for i, (trial, ax) in enumerate(zip(trials, ax_list)):
            what = self._TRIALS2KEY[trial]
            ax.set_ylabel(self._TRIALS2YLABEL[trial])
            minval, maxval = np.inf, -np.inf
            for acc in accuracies:
                col = acc + "_" + what
                legend = i == 0
                data = self[col]
                minval, maxval = min(minval, data.min()), max(maxval, data.max())
                data.plot(ax=ax, legend=legend, use_index=True, label=acc, **self.ACC2PLTOPTS[acc])
                #data.plot(ax=ax, kind="bar")

                if i == 0:
                    ax.legend(loc='best', shadow=True, frameon=True) #fancybox=True)

            ax.set_xticks(range(len(data.index)))
            ax.set_xticklabels(data.index)
            #ax.set_xticklabels([root for root, ext in map(os.path.splitext, data.index)])

            # Set ylimits
            #stepsize = None
            #if "gbrv" in trial:
            #    ax.hlines(0.0, 0, len(data.index))
            #    #start, end = -0.6, +0.6
            #    start, end = max(-0.6, minval), min(+0.6, maxval)
            #    if end - start < 0.05: end = start + 0.1
            #    ax.set_ylim(start, end)
            #    ax.yaxis.set_ticks(np.arange(start, end, 0.05))

            if trial == "deltafactor":
                #start, end = 0.0, 15
                start, end  = 0.0, min(15, maxval)
                ax.set_ylim(start, end)
                #ax.yaxis.set_ticks(np.arange(start, end, 0.1))

            #if stepsize is not None:
            #    start, end = ax.get_ylim()
            #    ax.yaxis.set_ticks(np.arange(start, end, stepsize))

            plt.setp(ax.xaxis.get_majorticklabels(), rotation=25)

        return fig


class DfGbrvDataFrame(DataFrame):
    """
    Extends pandas DataFrame adding helper functions.
    """
    @classmethod
    def from_pseudos(cls, pseudos, raise_if_none_dojoreport=False):
        """
        Build and return a pandas :class:`DataFrame` in the form.

            basename     deltafactor  df_prime  gbrv_bcc  gbrv_fcc  symbol   md5
            H-high.psp8  0.074830     1.258340  0.028904  0.024726  H        5863396c90149cbe12af496141bde0d0
            ...

        where `gbrv_bcc` and `gbrv_fcc` are the relative errors (in percentage) wrt the AE calculations.

	Args:
	    raise_if_none_dojoreport: If True, a ValueError is raised if one of the pseudo does not
                have the dojo_report else a warning is emitted.
        """

        _TRIALS2KEY = {
            "deltafactor": "dfact_meV",
            "gbrv_bcc": "a0_rel_err",
            "gbrv_fcc": "a0_rel_err",
        }

        rows = []
        for p in pseudos:
            # Extract the dojo_report
	    if not p.has_dojo_report:
		msg = "%s does not have the dojo_report" % p.filepath
		if not raise_if_none_dojoreport:
		    logger.warning(msg)
                    continue
		else:
		    raise ValueError(msg)

            report = p.dojo_report
            row = dict(basename=p.basename, symbol=p.symbol, md5=p.md5)

            for trial, key in _TRIALS2KEY.items():
                # Get results as function of ecut
                try:
                    data = report[trial]
                except KeyError:
                    print("%s does not have %s" % (p.basename, trial))
                    continue

                # Extract the value with highest ecut.
                high_ecut = list(data.keys())[-1]
                row.update({trial: data[high_ecut][key]})
                if trial == "deltafactor":
                    row.update(dict(df_prime=data[high_ecut]["dfactprime_meV"]))

            rows.append(row)

        return cls(rows)

    @add_fig_kwargs
    def plot_dfgbrv_dist(self, **kwargs):
        """
        Plot four distribution plots for the deltafactor, deltafactor prime and the
        relative errors for the GBRV fcc, bcc structures.

        Return: `matplotlib` figure.
        """
        import matplotlib.pyplot as plt
        fig, ax_list = plt.subplots(nrows=2, ncols=2, squeeze=True)
        ax_list = ax_list.ravel()

        import seaborn as sns
        for ax, col in zip(ax_list.ravel(), ["deltafactor", "gbrv_fcc", "df_prime", "gbrv_bcc"]):
            values = self[col].dropna()
            sns.distplot(values, ax=ax, rug=True, hist=True, kde=False, label=col, bins=kwargs.pop("bins", 50))

            # Add text with Mean or (MARE/RMSRE)
            text = []; app = text.append
            if col in ("deltafactor", "df_prime"):
                app("Mean = %.2f" % values.mean())
            else:
                app("MARE = %.2f" % values.abs().mean())
                app("RMSRE = %.2f" % np.sqrt((values**2).mean()))

            ax.text(0.8, 0.8, "\n".join(text), transform=ax.transAxes)

        return fig


def compute_dfact_entry(pseudo, num_sites, volumes, etotals):
    """
    This function computes the deltafactor and returns the dictionary to be inserted
    in the dojoreport file.

    Args:
        pseudo: Pseudopotential object.
        num_sites: Number of sites in unit cell
        volumes: List with unit cell volumes in Ang**3
        etotals: List of total energies in eV.

    Return:
        (outd, eos_fit)
        where outd is the Dictionary with results to be inserted in the djrepo file.
        eos_fit is the object storing the results of the EOS fit.
    """
    nan = float('NaN')

    outd = dict(
        etotals=list(etotals),
        volumes=list(volumes),
        num_sites=num_sites,
        dfact_meV=nan,
        dfactprime_meV=nan,
        v0=nan,
        b0=nan,
        b0_GPa=nan,
        b1=nan,
    )

    volumes = np.asarray(volumes)
    etotals = np.asarray(etotals)
    eos_fit = None

    try:
        # Use same fit as the one employed for the deltafactor.
        eos_fit = EOS.DeltaFactor().fit(volumes/num_sites, etotals/num_sites)

        # Get reference results (Wien2K).
        wien2k = df_database(pseudo.xc).get_entry(pseudo.symbol)

        # Compute deltafactor estimator.
        dfact = df_compute(wien2k.v0, wien2k.b0_GPa, wien2k.b1,
                           eos_fit.v0, eos_fit.b0_GPa, eos_fit.b1, b0_GPa=True)

        dfactprime_meV = dfact * (30 * 100) / (eos_fit.v0 * eos_fit.b0_GPa)

        dfres = {
            "dfact_meV": dfact,
            "dfactprime_meV": dfactprime_meV,
            "v0": eos_fit.v0,
            "b0": eos_fit.b0,
            "b0_GPa": eos_fit.b0_GPa,
            "b1": eos_fit.b1,
        }

        for k, v in dfres.items():
            v = v if not isinstance(v, complex) else nan
            dfres[k] = v

        outd.update(dfres)

    except EOS.Error as exc:
        outd["_exceptions"] = str(exc)

    return outd, eos_fit

