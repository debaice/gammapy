# Licensed under a 3-clause BSD style license - see LICENSE.rst
import copy
from collections import UserList
from pathlib import Path
import numpy as np
from astropy.units import Quantity
from ..utils.scripts import make_path
from ..utils.energy import EnergyBounds
from ..utils.table import table_from_row_data
from ..data import ObservationStats
from ..irf import EffectiveAreaTable, EnergyDispersion, IRFStacker
from .core import CountsSpectrum, PHACountsSpectrum, PHACountsSpectrumList
from .utils import SpectrumEvaluator

__all__ = [
    "SpectrumStats",
    "SpectrumObservation",
    "SpectrumObservationList",
]


class SpectrumStats(ObservationStats):
    """Spectrum stats.

    Extends `~gammapy.data.ObservationStats` with spectrum
    specific information (energy bin info at the moment).
    """

    def __init__(self, **kwargs):
        self.energy_min = kwargs.pop("energy_min", Quantity(0, "TeV"))
        self.energy_max = kwargs.pop("energy_max", Quantity(0, "TeV"))
        super().__init__(**kwargs)

    def __str__(self):
        ss = super().__str__()
        ss += "energy range: {:.2f} - {:.2f}".format(self.energy_min, self.energy_max)
        return ss

    def to_dict(self):
        """TODO: document"""
        data = super().to_dict()
        data["energy_min"] = self.energy_min
        data["energy_max"] = self.energy_max
        return data


class SpectrumObservation:
    """1D spectral analysis storage class.

    This container holds the ingredients for 1D region based spectral analysis.

    Meta data is stored in the ``on_vector`` attribute.
    This reflects the OGIP convention.

    Parameters
    ----------
    on_vector : `~gammapy.spectrum.PHACountsSpectrum`
        On vector
    aeff : `~gammapy.irf.EffectiveAreaTable`
        Effective Area
    off_vector : `~gammapy.spectrum.PHACountsSpectrum`, optional
        Off vector
    edisp : `~gammapy.irf.EnergyDispersion`, optional
        Energy dispersion matrix

    Examples
    --------
    ::

        from gammapy.spectrum import SpectrumObservation
        filename = '$GAMMAPY_DATA/joint-crab/spectra/hess/pha_obs23523.fits'
        obs = SpectrumObservation.read(filename)
        print(obs)
    """

    def __init__(self, on_vector, aeff=None, off_vector=None, edisp=None):
        self.on_vector = on_vector
        self.aeff = aeff
        self.off_vector = off_vector
        self.edisp = edisp

    def __str__(self):
        ss = self.total_stats_safe_range.__str__()
        return ss

    @property
    def obs_id(self):
        """Unique identifier"""
        return self.on_vector.obs_id

    @obs_id.setter
    def obs_id(self, obs_id):
        self.on_vector.obs_id = obs_id
        if self.off_vector is not None:
            self.off_vector.obs_id = obs_id

    @property
    def meta(self):
        """Meta information"""
        return self.on_vector.meta

    @property
    def livetime(self):
        """Dead-time corrected observation time"""
        return self.on_vector.livetime

    @property
    def alpha(self):
        """Exposure ratio between signal and background regions"""
        return self.on_vector.backscal / self.off_vector.backscal

    @property
    def e_reco(self):
        """Reconstruced energy bounds array."""
        return EnergyBounds(self.on_vector.energy.edges)

    @property
    def e_true(self):
        """True energy bounds array."""
        return EnergyBounds(self.aeff.energy.edges)

    @property
    def nbins(self):
        """Number of reconstruced energy bins"""
        return self.on_vector.energy.nbin

    @property
    def lo_threshold(self):
        """Low energy threshold"""
        return self.on_vector.lo_threshold

    @lo_threshold.setter
    def lo_threshold(self, threshold):
        self.on_vector.lo_threshold = threshold
        if self.off_vector is not None:
            self.off_vector.lo_threshold = threshold

    @property
    def hi_threshold(self):
        """High energy threshold"""
        return self.on_vector.hi_threshold

    @hi_threshold.setter
    def hi_threshold(self, threshold):
        self.on_vector.hi_threshold = threshold
        if self.off_vector is not None:
            self.off_vector.hi_threshold = threshold

    def reset_thresholds(self):
        """Reset energy thresholds (i.e. declare all energy bins valid)"""
        self.on_vector.reset_thresholds()
        if self.off_vector is not None:
            self.off_vector.reset_thresholds()

    def compute_energy_threshold(
        self, method_lo="none", method_hi="none", reset=False, **kwargs
    ):
        """Compute and set the safe energy threshold.

        Set the high and low energy threshold for each observation based on a
        chosen method.

        Available methods for setting the low energy threshold:

        * area_max : Set energy threshold at x percent of the maximum effective
          area (x given as kwargs['area_percent_lo'])

        * energy_bias : Set energy threshold at energy where the energy bias
          exceeds a value of x percent (given as kwargs['bias_percent_lo'])

        * none : Do not apply a lower threshold

        Available methods for setting the high energy threshold:

        * area_max : Set energy threshold at x percent of the maximum effective
          area (x given as kwargs['area_percent_hi'])

        * energy_bias : Set energy threshold at energy where the energy bias
          exceeds a value of x percent (given as kwargs['bias_percent_hi'])

        * none : Do not apply a higher energy threshold

        Parameters
        ----------
        method_lo : {'area_max', 'energy_bias', 'none'}
            Method for defining the low energy threshold
        method_hi : {'area_max', 'energy_bias', 'none'}
            Method for defining the high energy threshold
        reset : bool
            Reset existing energy thresholds before setting the new ones
            (default is `False`)
        """
        if reset:
            self.reset_thresholds()

        # It is important to update the low and high threshold for ON and OFF
        # vector, otherwise Sherpa will not understand the files

        # Low threshold
        if method_lo == "area_max":
            aeff_thres = kwargs["area_percent_lo"] / 100 * self.aeff.max_area
            thres_lo = self.aeff.find_energy(aeff_thres)
        elif method_lo == "energy_bias":
            thres_lo = self.edisp.get_bias_energy(kwargs["bias_percent_lo"] / 100)
        elif method_lo == "none":
            thres_lo = self.e_true[0]
        else:
            raise ValueError("Invalid method_lo: {}".format(method_lo))

        self.on_vector.lo_threshold = thres_lo
        if self.off_vector is not None:
            self.off_vector.lo_threshold = thres_lo

        # High threshold
        if method_hi == "area_max":
            aeff_thres = kwargs["area_percent_hi"] / 100 * self.aeff.max_area
            e_min = self.e_true[-1]
            thres_hi = self.aeff.find_energy(aeff_thres, emin=e_min)
        elif method_hi == "energy_bias":
            e_min = self.e_true[-1]
            thres_hi = self.edisp.get_bias_energy(
                kwargs["bias_percent_hi"] / 100, emin=e_min
            )
        elif method_hi == "none":
            thres_hi = self.e_true[-1]
        else:
            raise ValueError("Invalid method_hi: {}".format(method_hi))

        self.on_vector.hi_threshold = thres_hi
        if self.off_vector is not None:
            self.off_vector.hi_threshold = thres_hi

    @property
    def background_vector(self):
        """Background `~gammapy.spectrum.CountsSpectrum`.

        bkg = alpha * n_off

        If alpha is a function of energy this will differ from
        self.on_vector * self.total_stats.alpha because the latter returns an
        average value for alpha.
        """
        energy = self.off_vector.energy.edges
        data = self.off_vector.data.data * self.alpha
        return CountsSpectrum(data=data, energy_lo=energy[:-1], energy_hi=energy[1:])

    @property
    def excess_vector(self):
        """Excess `~gammapy.spectrum.CountsSpectrum`.

        excess = n_on = alpha * n_off
        """
        energy = self.off_vector.energy.edges
        data = self.on_vector.data.data - self.background_vector.data.data
        return CountsSpectrum(data=data, energy_lo=energy[:-1], energy_hi=energy[1:])

    @property
    def total_stats(self):
        """Return total `~gammapy.spectrum.SpectrumStats`
        """
        return self.stats_in_range(0, self.nbins - 1)

    @property
    def total_stats_safe_range(self):
        """Return total `~gammapy.spectrum.SpectrumStats` within the tresholds
        """
        safe_bins = self.on_vector.bins_in_safe_range
        return self.stats_in_range(safe_bins[0], safe_bins[-1])

    def stats_in_range(self, bin_min, bin_max):
        """Compute stats for a range of energy bins.

        Parameters
        ----------
        bin_min, bin_max: int
            Bins to include

        Returns
        -------
        stats : `~gammapy.spectrum.SpectrumStats`
            Stacked stats
        """
        idx = np.arange(bin_min, bin_max + 1)
        stats_list = [self.stats(ii) for ii in idx]
        stacked_stats = SpectrumStats.stack(stats_list)
        stacked_stats.livetime = self.livetime
        stacked_stats.gamma_rate = stacked_stats.excess / stacked_stats.livetime
        stacked_stats.obs_id = self.obs_id
        stacked_stats.energy_min = self.e_reco[bin_min]
        stacked_stats.energy_max = self.e_reco[bin_max + 1]
        return stacked_stats

    def stats(self, idx):
        """Compute stats for one energy bin.

        Parameters
        ----------
        idx : int
            Energy bin index

        Returns
        -------
        stats : `~gammapy.spectrum.SpectrumStats`
            Stats
        """
        if self.off_vector is not None:
            n_off = int(self.off_vector.data.data.value[idx])
            a_off = self.off_vector._backscal_array[idx]
        else:
            n_off = 0
            a_off = 1  # avoid zero division error

        return SpectrumStats(
            energy_min=self.e_reco[idx],
            energy_max=self.e_reco[idx + 1],
            n_on=int(self.on_vector.data.data.value[idx]),
            n_off=n_off,
            a_on=self.on_vector._backscal_array[idx],
            a_off=a_off,
            obs_id=self.obs_id,
            livetime=self.livetime,
        )

    def stats_table(self):
        """Per-bin stats as a table.

        Returns
        -------
        table : `~astropy.table.Table`
            Table with stats for one energy bin in one row.
        """
        rows = [self.stats(idx).to_dict() for idx in range(len(self.e_reco) - 1)]
        return table_from_row_data(rows=rows)

    def predicted_counts(self, model):
        """Calculated number of predicted counts given a model.

        Parameters
        ----------
        model : `~gammapy.spectrum.models.SpectralModel`
            Spectral model

        Returns
        -------
        npred : `~gammapy.spectrum.CountsSpectrum`
            Predicted counts
        """
        predictor = SpectrumEvaluator(
            model=model, edisp=self.edisp, aeff=self.aeff, livetime=self.livetime
        )
        return predictor.compute_npred()

    @classmethod
    def read(cls, filename):
        """Read `~gammapy.spectrum.SpectrumObservation` from OGIP files.

        BKG file, ARF, and RMF must be set in the PHA header and be present in
        the same folder.

        Parameters
        ----------
        filename : str
            OGIP PHA file to read
        """
        filename = make_path(filename)
        dirname = filename.parent
        on_vector = PHACountsSpectrum.read(filename)
        rmf, arf, bkg = on_vector.rmffile, on_vector.arffile, on_vector.bkgfile

        try:
            energy_dispersion = EnergyDispersion.read(str(dirname / rmf))
        except IOError:
            # TODO : Add logger and echo warning
            energy_dispersion = None

        try:
            off_vector = PHACountsSpectrum.read(str(dirname / bkg))
        except IOError:
            # TODO : Add logger and echo warning
            off_vector = None

        effective_area = EffectiveAreaTable.read(str(dirname / arf))

        return cls(
            on_vector=on_vector,
            aeff=effective_area,
            off_vector=off_vector,
            edisp=energy_dispersion,
        )

    def write(self, outdir=None, use_sherpa=False, overwrite=False):
        """Write OGIP files.

        If you want to use the written files with Sherpa you have to set the
        ``use_sherpa`` flag. Then all files will be written in units 'keV' and
        'cm2'.

        Parameters
        ----------
        outdir : `pathlib.Path`
            output directory, default: pwd
        use_sherpa : bool, optional
            Write Sherpa compliant files, default: False
        overwrite : bool
            Overwrite existing files?
        """
        outdir = Path.cwd() if outdir is None else Path(outdir)
        outdir.mkdir(exist_ok=True, parents=True)

        phafile = self.on_vector.phafile
        bkgfile = self.on_vector.bkgfile
        arffile = self.on_vector.arffile
        rmffile = self.on_vector.rmffile

        self.on_vector.write(outdir / phafile, overwrite=overwrite, use_sherpa=use_sherpa)
        self.aeff.write(outdir / arffile, overwrite=overwrite, use_sherpa=use_sherpa)

        if self.off_vector is not None:
            self.off_vector.write(outdir / bkgfile, overwrite=overwrite, use_sherpa=use_sherpa)
        if self.edisp is not None:
            self.edisp.write(str(outdir / rmffile), overwrite=overwrite, use_sherpa=use_sherpa)

    def peek(self, figsize=(10, 10)):
        """Quick-look summary plots."""
        import matplotlib.pyplot as plt

        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(nrows=2, ncols=2, figsize=figsize)

        ax1.set_title("Counts")
        energy_unit = "TeV"
        if self.off_vector is not None:
            self.background_vector.plot_hist(
                ax=ax1, label="alpha * n_off", color="darkblue", energy_unit=energy_unit
            )
        self.on_vector.plot_hist(
            ax=ax1,
            label="n_on",
            color="darkred",
            energy_unit=energy_unit,
            show_energy=(self.hi_threshold, self.lo_threshold),
        )
        ax1.set_xlim(
            0.7 * self.lo_threshold.to_value(energy_unit),
            1.3 * self.hi_threshold.to_value(energy_unit),
        )
        ax1.legend(numpoints=1)

        ax2.set_title("Effective Area")
        e_unit = self.aeff.energy.unit
        self.aeff.plot(ax=ax2, show_energy=(self.hi_threshold, self.lo_threshold))
        ax2.set_xlim(
            0.7 * self.lo_threshold.to_value(e_unit),
            1.3 * self.hi_threshold.to_value(e_unit),
        )

        ax3.axis("off")
        if self.off_vector is not None:
            ax3.text(0, 0.2, "{}".format(self.total_stats_safe_range), fontsize=12)

        ax4.set_title("Energy Dispersion")
        if self.edisp is not None:
            self.edisp.plot_matrix(ax=ax4)

        # TODO: optimize layout
        plt.subplots_adjust(wspace=0.3)

    def to_sherpa(self):
        """Convert to `~sherpa.astro.data.DataPHA`.

        Associated background vectors and IRFs are also translated to sherpa
        objects and appended to the PHA instance.
        """
        pha = self.on_vector.to_sherpa(name="pha_obs{}".format(self.obs_id))
        if self.aeff is not None:
            arf = self.aeff.to_sherpa(name="arf_obs{}".format(self.obs_id))
        else:
            arf = None
        if self.edisp is not None:
            rmf = self.edisp.to_sherpa(name="rmf_obs{}".format(self.obs_id))
        else:
            rmf = None

        pha.set_response(arf, rmf)

        if self.off_vector is not None:
            bkg = self.off_vector.to_sherpa(name="bkg_obs{}".format(self.obs_id))
            bkg.set_response(arf, rmf)
            pha.set_background(bkg, 1)

        # see https://github.com/sherpa/sherpa/blob/36c1f9dabb3350b64d6f54ab627f15c862ee4280/sherpa/astro/data.py#L1400
        pha._set_initial_quantity()
        return pha

    def copy(self):
        """A deep copy."""
        return copy.deepcopy(self)

    def to_spectrum_dataset(self):
        """Creates a SpectrumDatasetOnOff from a SpectrumObservation object"""
        from .dataset import SpectrumDatasetOnOff, SpectrumDataset

        quality = self.on_vector.quality
        mask = quality == 0

        if self.off_vector is not None:
            # Build mask from quality vector
            dataset = SpectrumDatasetOnOff(
                counts_on=self.on_vector,
                aeff=self.aeff,
                counts_off=self.off_vector,
                edisp=self.edisp,
                livetime=self.livetime,
                mask=mask,
            )
        else:
            dataset = SpectrumDataset(
                counts=self.on_vector,
                aeff=self.aeff,
                edisp=self.edisp,
                livetime=self.livetime,
                mask=mask,
            )

        return dataset



class SpectrumObservationList(UserList):
    """List of `~gammapy.spectrum.SpectrumObservation` objects."""

    def __str__(self):
        ss = self.__class__.__name__
        ss += "\nNumber of observations: {}".format(len(self))
        # ss += '\n{}'.format(self.obs_id)
        return ss

    def obs(self, obs_id):
        """Return one observation.

        Parameters
        ----------
        obs_id : int
            Identifier
        """
        obs_id_list = [o.obs_id for o in self]
        idx = obs_id_list.index(obs_id)
        return self[idx]

    @property
    def obs_id(self):
        """List of observations ids"""
        return [o.obs_id for o in self]

    @property
    def total_livetime(self):
        """Summed livetime"""
        livetimes = [o.livetime.to_value("s") for o in self]
        return Quantity(np.sum(livetimes), "s")

    @property
    def on_vector_list(self):
        """On `~gammapy.spectrum.PHACountsSpectrumList`"""
        return PHACountsSpectrumList([o.on_vector for o in self])

    @property
    def off_vector_list(self):
        """Off `~gammapy.spectrum.PHACountsSpectrumList`"""
        return PHACountsSpectrumList([o.off_vector for o in self])

    def stack(self):
        """Return stacked `~gammapy.spectrum.SpectrumObservation`"""
        stacker = SpectrumObservationStacker(obs_list=self)
        stacker.run()
        return stacker.stacked_obs

    def safe_range(self, method="inclusive"):
        """Safe energy range

        This is the energy range in with any / all observations have their safe
        threshold

        Parameters
        ----------
        method : str, {'inclusive', 'exclusive'}
            Maximum or minimum range
        """
        unit = "TeV"
        lo = [obs.lo_threshold.to_value(unit) for obs in self]
        hi = [obs.hi_threshold.to_value(unit) for obs in self]

        if method == "inclusive":
            return Quantity([min(lo), max(hi)], unit)
        elif method == "exclusive":
            return Quantity([max(lo), min(hi)], unit)
        else:
            raise ValueError("Invalid method: {}".format(method))

    def write(self, outdir=None, pha_typeII=False, **kwargs):
        """Create OGIP files

        Each observation will be written as seperate set of FITS files by
        default. If the option ``pha_typeII`` is enabled all on and off counts
        spectra will be collected into one
        `~gammapy.spectrum.PHACountsSpectrumList` and written to one FITS file.
        All datasets will be associated to the same response files.
        see
        https://heasarc.gsfc.nasa.gov/docs/heasarc/ofwg/docs/spectra/ogip_92_007/node8.html

        TODO: File written with the ``pha_typeII`` option are not read
        properly with sherpa. This could be a sherpa issue. Investigate and
        file issue.

        Parameters
        ----------
        outdir : str, `pathlib.Path`, optional
            Output directory, default: pwd
        pha_typeII : bool, default: False
            Collect PHA datasets into one file
        """
        outdir = make_path(outdir)
        outdir.mkdir(exist_ok=True, parents=True)
        if not pha_typeII:
            for obs in self:
                obs.write(outdir=outdir, **kwargs)
        else:
            onlist = self.on_vector_list
            onlist.write(outdir / "pha2.fits", **kwargs)
            offlist = self.off_vector_list
            # This filename is hardcoded since it is a column in the on list
            offlist.write(outdir / "bkg.fits", **kwargs)
            arf_file = onlist.to_table().meta["ancrfile"]
            rmf_file = onlist.to_table().meta["respfile"]
            self[0].aeff.write(outdir / arf_file, **kwargs)
            self[0].edisp.write(outdir / rmf_file, **kwargs)

    @classmethod
    def read(cls, directory, pha_typeII=False):
        """Read multiple observations

        This methods reads all PHA files contained in a given directory. Enable
        ``pha_typeII`` to read a PHA type II file.

        see
        https://heasarc.gsfc.nasa.gov/docs/heasarc/ofwg/docs/spectra/ogip_92_007/node8.html

        TODO: Replace with more sophisticated file managment system

        Parameters
        ----------
        directory : `pathlib.Path`
            Directory holding the observations
        pha_typeII : bool, default: False
            Read PHA typeII file
        """
        obs_list = cls()
        directory = make_path(directory)

        if not pha_typeII:
            # glob default order depends on OS, so we call sorted() explicitely to
            # get reproducable results
            filelist = sorted(directory.glob("pha*.fits"))
            for phafile in filelist:
                obs = SpectrumObservation.read(phafile)
                obs_list.append(obs)
        else:
            # NOTE: filenames for type II PHA files are hardcoded
            on_vectors = PHACountsSpectrumList.read(directory / "pha2.fits")
            off_vectors = PHACountsSpectrumList.read(directory / "bkg.fits")
            aeff = EffectiveAreaTable.read(directory / "arf.fits")
            edisp = EnergyDispersion.read(directory / "rmf.fits")

            for on, off in zip(on_vectors, off_vectors):
                obs = SpectrumObservation(
                    on_vector=on, off_vector=off, aeff=aeff, edisp=edisp
                )
                obs_list.append(obs)

        return obs_list

    def peek(self):
        """Quickly look at observations

        Uses IPython widgets.
        TODO: Change to bokeh
        """
        from ipywidgets import interact

        max_ = len(self) - 1

        def show_obs(idx):
            self[idx].peek()

        return interact(show_obs, idx=(0, max_, 1))


    def to_spectrum_datasets(self, model=None, fit_range=None, forward_folded=True):
        """Creates a list of SpectrumDatasetOnOff

        Parameters
        ----------
        model : `~gammapy.spectrum.models.SpectralModel`
            Spectral model to use for all datasets.
        forward_folded : bool, default: True
            Fold ``model`` with the IRFs given in ``obs_list``
        fit_range : tuple of `~astropy.units.Quantity`
            The intersection between the fit range and the observation thresholds will be used.
            If you want to control which bins are taken into account in the fit for each
            observation, use :func:`~gammapy.spectrum.PHACountsSpectrum.quality`

        """
        from ..utils.fitting.datasets import Datasets
        datasets = []

        for obs in self:
            dataset = obs.to_spectrum_dataset()
            if not forward_folded:
                dataset.edisp = None
            dataset.model = model
            datasets.append(dataset)

        if fit_range is not None:
            energy = dataset.counts_on.energy.edges
            mask = (energy[:-1] >= fit_range[0]) & (energy[1:] <= fit_range[1])
        else:
            mask = None

        return Datasets(datasets, mask=mask)
