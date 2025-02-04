"""----------------------------------------------------------------------
PyBMFT-C: Bay-Marsh-Forest Transect Carbon Model (Python version)

A Python version of the Coastal Landscape Transect model (CoLT)
from Valentine et al. (2023): A dynamic model for the moprhological
evolution of a backbarrier basin with marshes, mudflats, and an
upland slope, and their requisite carbon pools.

This Python version (PyBMFT-C) is used in the BarrierBMFT coupled model
framework. See README documentation for descriptions of discrepencies
with original CoLT Matlab code.

Last updated _16 August 2022_ by _IRB Reeves_
----------------------------------------------------------------------"""

import numpy as np
import scipy.io
from scipy.integrate import solve_ivp
import math
import bisect
import matplotlib.pyplot as plt

from buildtransect import buildtransect
from funBAY import funBAY
from funBAY import POOLstopp5
from calcFE import calcFE
from evolvemarsh import evolvemarsh
from decompose import decompose


class Bmftc:
    def __init__(
            self,
            name="default",
            time_step=1,
            time_step_count=100,
            relative_sea_level_rise=4,
            reference_concentration=10,
            slope_upland=0.005,

            marsh_width_initial=1000,
            bay_fetch_initial=5000,
            forest_width_initial_fixed=False,
            forest_width_initial=2000,
            forest_age_initial=60,
            filename_marshspinup="Input/PyBMFT-C/MarshStrat_all_RSLR1_CO50.mat",
            filename_equilbaydepth="Input/PyBMFT-C/Equilibrium Bay Depth.mat",

            # KV Organic
            bulk_density_mineral=2000,
            bulk_density_organic=85,
            tidal_period=12.5 * 3600 * 1,
            settling_velocity_effective=0.05 * 10 ** (-3),
            settling_velocity_mudflat=0.5 * 10 ** (-3),
            critical_shear_mudflat=0.1,
            wind_speed=6,
            tidal_amplitude=1.4 / 2,
            marsh_progradation_coeff=2,
            marsh_erosion_coeff=0.16 / (365 * 24 * 3600),
            mudflat_erodibility_coeff=0.0001,
            dist_marsh_bank=10,
            tide_cycles_yearly=365 * (24 / 12.5),

            # Vegetation
            maximum_biomass_marsh=2500,
            veg_minimum_depth=0,
            maximum_biomass_forest=5000,
            tree_biomass_forest_edge=4,
            tree_growth_rate=2,
            forest_background_carbon_accumulation=0.0001,
            forest_carbon_layer_wetted_soils=5,
            forest_belowground_decay_constant=2,
            zero_decomposition_depth_marsh=0.4,
            decomposition_coefficient_marsh=0.1,
            forest_on=True,

            # Bay/marsh
            tidal_iterations=500,
            mineral_flux_bay_to_marsh=0,
            organic_flux_bay_to_marsh=0,
            sed_flux_pond=0,

    ):
        """Bay-Marsh-Forest Transect Carbon Model (Python version)

        Parameters
        ----------
        name: string, optional
            Name of simulation
        time_step: float, optional
            Time step of the numerical model [yr]

        Examples
        --------
        >>> from bmftc import Bmftc
        >>> model = Bmftc()
        """

        self._name = name
        self._RSLRi = relative_sea_level_rise  # [mm/yr]
        self._RSLR = relative_sea_level_rise * 10 ** (-3) / (3600 * 24 * 365)  # Convert from mm/yr to m/s
        self._time_index = 0
        self._dt = time_step
        self._dur = time_step_count + 1
        self._Coi = reference_concentration  # [mg/L]
        self._Co = reference_concentration / 1000  # Convert to kg/m3
        self._slope = slope_upland

        self._mwo = marsh_width_initial
        self._bfo = bay_fetch_initial
        self._forest_width_initial_fixed = forest_width_initial_fixed  # [Boolean] Determines whether simulation auto-calculates initial forest width based on RSLR/slope (False) or starts with fixed width (True)
        self._forest_width_initial = forest_width_initial  # Fixed initial width of forest, applied only if forest_width_initial_fixed = True
        self._startforestage = forest_age_initial

        self._rhos = bulk_density_mineral  # [kg/m3]
        self._rhoo = bulk_density_organic  # [kg/m3]
        self._P = tidal_period
        self._ws = settling_velocity_effective
        self._wsf = settling_velocity_mudflat
        self._tcr = critical_shear_mudflat
        self._wind = wind_speed
        self._amp = tidal_amplitude
        self._Ba = marsh_progradation_coeff
        self._Be = marsh_erosion_coeff
        self._lamda = mudflat_erodibility_coeff
        self._dist = dist_marsh_bank
        self._cyclestep = tide_cycles_yearly

        self._BMax = maximum_biomass_marsh
        self._Dmin = veg_minimum_depth
        self._Bmax_forest = maximum_biomass_forest
        self._a = tree_biomass_forest_edge  # [g/m2] Tree biomass value at marsh-forest boundary (amount of carbon in transition zone from trees)
        self._b = tree_growth_rate  # Growth rate of trees
        self._f0 = forest_background_carbon_accumulation  # [g/m2/yr] Background carbon accumulation in the soils accross entire forest
        self._fwet = forest_carbon_layer_wetted_soils  # Forest carbon layer from wetted soils
        self._fgrow = forest_belowground_decay_constant  # Exponential decay constant for calculation of belowground forest carbon
        self._mui = zero_decomposition_depth_marsh  # [m] Depth below which decomposition goes to zero in the marsh
        self._mki = decomposition_coefficient_marsh  # Coefficient of decomposition in the marsh
        self._numiterations = tidal_iterations

        self._Fm_min = mineral_flux_bay_to_marsh  # [kg/yr] Mass flux of mineral sediment from the bay to the marsh
        self._Fm_org = organic_flux_bay_to_marsh  # [kg/yr] Mass flux of organic sediment from the bay to the marsh
        self._Fp_sum = sed_flux_pond  # Amount of sediment taken from ponds to recharge sedimentation to drowning interior marsh

        # Calculate additional variables
        self._SLR = self._RSLR * (3600 * 24 * 365)  # Convert to m/yr
        self._rhou = 1 / ((1 - 0.05) / self._rhos + 0.05 / self._rhoo)  # Bulk density of underlying bay, 95% mineral, 5% organic
        self._rhob = self._rhou
        self._tr = self._amp * 2  # [m] Tidal range
        self._Dmax = 0.7167 * 2 * self._amp - 0.483  # [m] Maximum depth below high water that marsh veg can grow

        # Load MarshStrat spin up file
        marsh_spinup = scipy.io.loadmat(filename_marshspinup)
        self._elev25 = marsh_spinup["elev_25"]
        self._min_25 = marsh_spinup["min_25"]
        self._orgAL_25 = marsh_spinup["orgAL_25"]
        self._orgAT_25 = marsh_spinup["orgAT_25"]

        # Load Forest Organic Profile files: Look-up table with soil organic matter for forest based on age and depth
        directory_fop = "Input/PyBMFT-C/Forest_Organic_Profile"
        file_forestOM = scipy.io.loadmat(directory_fop + "/forestOM.mat")  # [g] Table with forest organic matter profile stored in 25 depth increments of 2.5cm (rows) for forests of different ages (columns) from 1 to 80 years
        self._forestOM = file_forestOM["forestOM"]
        file_forestMIN = scipy.io.loadmat(directory_fop + "/forestMIN.mat")  # [g] Table with forest mineral matter profile stored in 25 depth increments of 2.5cm (rows) for forests of different ages (columns) from 1 to 80 years
        self._forestMIN = file_forestMIN["forestMIN"]
        file_B_rts = scipy.io.loadmat(directory_fop + "/B_rts.mat")
        self._B_rts = file_B_rts["B_rts"]

        # Continue variable initializations
        self._startyear = np.size(self._elev25, axis=0)
        self._endyear = self._dur + self._startyear

        self._msl = np.zeros([self._endyear])
        self._msl[self._startyear:self._endyear] = np.linspace(1, self._dur, num=self._dur) * self._SLR  # [m] Mean sea level over time relative to start

        # Time
        self._to = np.linspace(0, 3600 * 24 * 365 * 1, 2)
        self._timestep = 365 * (24 / 12.5)  # [tidal cycles per year] number to multiply accretion simulated over a tidal cycle by

        # Initialize bay, marsh, and forest edge variables
        self._x_b = 0  # First bay cell
        self._x_m = math.ceil(self._bfo)  # First marsh cell
        self._Marsh_edge = np.zeros([self._endyear])
        self._Marsh_edge[:self._startyear] = self._x_m
        self._Forest_edge = np.zeros(self._endyear)
        self._fetch = np.zeros([self._endyear])
        self._fetch[:self._startyear] = self._bfo
        self._forest_on = forest_on  # Boolean controls whether forest organic deposition/decomposition occurs

        self._tidal_dt = self._P / self._numiterations  # Inundation time
        self._OCb = np.zeros(self._endyear)  # Organic content of uppermost layer of bay sediment, which determines the organic content of suspended material deposited onto the marsh. Initially set to zero.
        self._OCb[:self._endyear + 1] = 0.05
        self._edge_flood = np.zeros(self._endyear)  # Annual count of marsh edge cells flooded
        self._Edge_ht = np.zeros(self._endyear)  # [m] Height of marsh scarp above MHW

        self._marshOM_initial = (np.sum(np.sum(self._orgAL_25)) + np.sum(np.sum(self._orgAT_25))) / 1000  # [kg] Total mass of organic matter in the marsh at the beginning of the simulation (both alloch and autoch)
        self._marshMM_initial = np.sum(np.sum(self._min_25)) / 1000  # [kg] Total mass of mineral matter in the marsh at the beginning of the simulation
        self._marshLOI_initial = self._marshOM_initial / (self._marshOM_initial + self._marshMM_initial) * 100  # [%] LOI of the initial marsh deposit
        self._marshOCP_initial = 0.4 * self._marshLOI_initial + 0.0025 * self._marshLOI_initial ** 2  # [%] Organic carbon content from Craft et al. (1991)
        self._marshOC_initial = self._marshOCP_initial / 100 * (self._marshOM_initial + self._marshMM_initial)  # [kg] Organic carbon deposited in the marsh over the past spinup years

        # Build starting transect
        self._B, self._db, self._elevation = buildtransect(self._RSLRi, self._Coi, self._slope, self._mwo, self._elev25, self._amp, self._wind, self._bfo, self._endyear, self._startyear, filename_equilbaydepth, self._forest_width_initial_fixed, self._forest_width_initial, plot=False)

        # Find first forest cell x-location
        self._x_f = bisect.bisect_left(self._elevation[self._startyear - 1, :], self._msl[self._startyear] + self._amp - self._Dmin + 0.03)  # First forest cell

        # Set up vectors for deposition
        self._organic_dep_alloch = np.zeros([self._endyear, self._B])
        self._organic_dep_autoch = np.zeros([self._endyear, self._B])
        self._mineral_dep = np.zeros([self._endyear, self._B])
        self._organic_dep_alloch[:self._startyear, self._x_m: self._x_m + self._mwo] = self._orgAL_25  # Set spinup years to be the spin up values for deposition
        self._organic_dep_autoch[:self._startyear, self._x_m: self._x_m + self._mwo] = self._orgAT_25
        self._mineral_dep[:self._startyear, self._x_m: self._x_m + self._mwo] = self._min_25

        # Set options for ODE solver
        POOLstopp5.terminal = True

        # Calculate where elevation is right for the forest to start
        self._Forest_edge[self._startyear - 1] = bisect.bisect_left(self._elevation[self._startyear - 1, :], self._msl[self._startyear - 1] + self._amp + self._Dmin)
        self._forestage = self._startforestage

        self._Bay_depth = np.zeros([self._endyear])
        self._Bay_depth[:self._startyear] = self._db
        self._dmo = self._elevation[self._startyear - 1, self._x_m]  # Set marsh edge depth to the elevation of the marsh edge at startyear

        # Initialize
        self._C_e_ODE = []
        self._Fc_ODE = []
        self._drown_break = 0
        self._Fow_min = 0  # [kg/yr] Annual net flux of mineral sediment into the bay from overwash

        # Initialize additional data storage arrays
        self._mortality = np.zeros([self._endyear, self._B])
        self._BayExport = np.zeros([self._endyear, 2])
        self._BayOM = np.zeros([self._endyear])
        self._BayMM = np.zeros([self._endyear])
        self._fluxes = np.zeros([8, self._endyear])
        self._bgb_sum = np.zeros([self._endyear])  # [g] Sum of organic matter deposited across the marsh platform in a given year
        self._Fd = np.zeros([self._endyear])  # [kg] Flux of organic matter out of the marsh due to decomposition
        self._avg_accretion = np.zeros([self._endyear])  # [m/yr] Annual accretion rate averaged across the marsh platform
        self._rhomt = np.zeros([self._dur])
        self._massmt = np.zeros([self._dur])
        self._C_e = np.zeros([self._endyear])
        self._aboveground_forest = np.zeros([self._endyear, self._B])  # Forest aboveground biomass
        self._OM_sum_au = np.zeros([self._endyear, self._B])
        self._OM_sum_al = np.zeros([self._endyear, self._B])
        self._BaySedDensity = np.zeros([self._dur])

    def update(self):
        """Update Bmftc by a single time step"""

        # Year including spinup
        yr = self._time_index + self._startyear

        # Calculate the density of the marsh edge cell
        try:
            boundyr_list = [i for i, x in enumerate(self._elevation[:yr, self._x_m]) if x < (self._msl[yr - 1] + self._amp - self._db)]
        except:
            boundyr_list = []
        if len(boundyr_list) >= 1:
            boundyr = boundyr_list[-1] + 1  # Most recent year where elevation of marsh edge has just risen above depth of erosion (i.e., bay bottom elevation): this is an ALTERATION/NEW ADDITION not included in original Matlab CoLT version
            usmass = 0  # [kg] Mass of sediment underlying marsh at marsh edge
        else:
            boundyr = 0
            us = self._elevation[0, self._x_m] - (self._msl[yr - 1] + self._amp - self._db)
            usmass = us * self._rhou  # [kg] Mass of sediment underlying marsh at marsh edge

        # Mass of sediment to be eroded at the current marsh edge above the depth of erosion [kg], constrained by boundyr: this is an ALTERATION/NEW ADDITION not included in original Matlab CoLT version
        massm = np.sum(self._organic_dep_autoch[boundyr:, self._x_m]) / 1000 + np.sum(self._organic_dep_alloch[boundyr:, self._x_m]) / 1000 + np.sum(self._mineral_dep[boundyr:, self._x_m]) / 1000 + usmass
        # Volume of sediment to be eroded at the current marsh edge above the depth of erosion [m3]
        volm = self._elevation[yr - 1, self._x_m] - (self._msl[yr - 1] + self._amp - self._db)

        rhom = massm / volm  # [kg/m3] Bulk density of marsh edge
        if rhom > self._rhos:
            rhom = self._rhos
        elif rhom < self._rhoo:
            rhom = self._rhoo
        self._rhomt[self._time_index] = rhom
        self._massmt[self._time_index] = massm

        Fm = (self._Fm_min + self._Fm_org) / (3600 * 24 * 365)  # [kg/s] Mass flux of both mineral and organic sediment from the bay to the marsh

        # Parameters to feed into ODE
        PAR = [
            self._rhos,
            self._P,
            self._B,
            self._wsf,
            self._tcr,
            self._Co,
            self._wind,
            self._Ba,
            self._Be,
            self._amp,
            self._RSLR,
            Fm,             # variable
            self._lamda,
            self._dist,
            self._dmo,      # variable
            self._rhob,     # variable
            rhom,           # variable
            self,
        ]

        # ODE solves for change in bay depth and width
        # IR 5July21: Small deviations in the solved values from the Matlab version (on the order of ~ 10^-4 to 10^-5)
        try:
            ode = solve_ivp(funBAY,
                            t_span=self._to,
                            y0=[self._bfo, self._db],
                            atol=10 ** (-6),
                            rtol=10 ** (-6),
                            method='BDF',
                            args=PAR,
                            )

            fetch_ODE = ode.y[0, :]
            db_ODE = ode.y[1, :]
        except ValueError:  # IR 25Feb22: Temprorary fix for rare ODE bug
            print("  <-- ODE Value Error: RSLR", self.RSLRi, " Co", self._Coi)
            fetch_ODE = [self._bfo]
            db_ODE = [self._db]
        except OverflowError:
            print("  <-- ODE Overflow Error: RSLR", self.RSLRi, " Co", self._Coi)
            fetch_ODE = [self._bfo]
            db_ODE = [self._db]

        self._db = db_ODE[-1]  # Set initial depth of the bay to final depth from funBAY
        self._C_e[yr] = self._C_e_ODE[-1]  # SSC at marsh edge (kg/m3)

        if self.x_b < 0:
            x_b_int = math.floor(self._x_b)
        else:
            x_b_int = math.ceil(self._x_b)

        target_x_m = math.ceil(fetch_ODE[-1]) + x_b_int  # New (potential) first marsh cell
        if target_x_m >= self._x_f:  # Forest or bayside barrier edge (i.e., upland MHW shoreline) cannot erode from bay processes
            self._bfo = self._bfo + (self._x_f - self._x_m) - 1  # Marsh edge can't be greater than or equal to forest edge
        else:
            self._bfo = fetch_ODE[-1]  # Set new fetch from funBAY

        Fc = self._Fc_ODE[-1] * 3600 * 24 * 365  # [kg/yr] Annual net flux of sediment out of/into the bay from outside the system
        Fc_org = Fc * self._OCb[yr - 1]  # [kg/yr] Annual net flux of organic sediment out of/into the bay from outside the system
        Fc_min = Fc * (1 - self._OCb[yr - 1])  # [kg/yr] Annual net flux of mineral sediment out of/into the bay from outside the system

        # Calculate the flux of organic and mineral sediment to the bay from erosion of the marsh
        Fe_org, Fe_min = calcFE(self._bfo, self._fetch[yr - 1], self._elevation, yr, self._organic_dep_autoch, self._organic_dep_alloch, self._mineral_dep, self._rhou, self._x_b, self._msl, self._amp, self._db)
        Fe_org /= 1000  # [kg/yr] Annual net flux of organic sediment to the bay due to erosion
        Fe_min /= 1000  # [kg/yr] Annual net flux of mineral sediment to the bay due to erosion

        Fb_org = Fe_org - self._Fm_org - Fc_org  # [kg/yr] Net flux of organic sediment into (or out of, if negative) the bay
        Fb_min = Fe_min - self._Fm_min - Fc_min + self._Fow_min  # [kg/yr] Net flux of mineral sediment into (or out of, if negative) the bay

        self._BayExport[yr, :] = [Fc_org, Fc_min]  # [kg/yr] Mass of organic and mineral sediment exported from the bay each year
        self._BayOM[yr] = Fb_org  # [kg/yr] Mass of organic sediment stored in the bay in each year
        self._BayMM[yr] = Fb_min  # [kg/yr] Mass of mineral sediment stored in the bay in each year

        # if Fb_org > 0 and Fb_min > 0:
        #     self._OCb[yr] = Fb_org / (Fb_org + Fb_min) + 0.05  # BIG CHANGE HERE
        # elif Fb_org > 0:
        #     self._OCb[yr] = 1  # 100% organic
        # elif Fb_min > 0:
        #     self._OCb[yr] = 0  # 100% mineral
        # else:
        #     self._OCb[yr] = self._OCb[yr - 1]
        #
        # # If bay has eroded down to depth below initial bay bottom, there is only mineral sediment remaining
        # # if self._db > self._Bay_depth[0]:  # Sign flipped: this way it does what the comment above says it's supposed to do
        # # if self._msl[yr] + self._amp - self._db < (self._msl[0] + self._amp - self._Bay_depth[0]):  # This version is based on elevations, not depths
        # if self._db < self._Bay_depth[0]:
        #     self._OCb[yr] = 0.05

        self._OCb[yr] = 0.05  # IR hardwired 20Apr22: prevents OCb from getting really large over long (>150 yr) runs

        self._rhob = 1 / ((1 - self._OCb[yr - 1]) / self._rhos + self._OCb[yr - 1] / self._rhoo)  # [kg/m3] Density of bay sediment

        if int(self._bfo) <= 10:
            self._drown_break = 1
            print("PyBMFT-C: Marsh has completely filled the basin.")
            self._endyear = yr
            return  # Exit program

        self._x_m = math.ceil(self._bfo) + x_b_int  # New first marsh cell
        try:
            self._x_f = max(self._x_m + 1, np.where(self._elevation[yr - 1, :] > self._msl[yr] + self._amp - self._Dmin + 0.03)[0][0])
        except IndexError:
            self._x_f = self._B
            self._drown_break = 1  # If x_f can't be found, barrier has drowned
            print("PyBMFT-C: Barrier has drowned.")
            self._endyear = yr
            return  # Exit program

        tempelevation = self._elevation[yr - 1, self._x_m: self._x_f + 1]
        Dcells = int(self._Marsh_edge[yr - 1] - self._x_m)  # Gives the change in the number of marsh cells

        if Dcells > 0:  # Prograde the marsh, with new marsh cells having the same elevation as the previous marsh edge
            tempelevation[0: int(Dcells)] = self._elevation[yr - 1, int(self._Marsh_edge[yr - 1])]
            # Account for mineral and organic material deposited in new marsh cells: this is an ALTERATION/NEW ADDITION not included in original Matlab CoLT version
            new_marsh_height = self._db  # New marsh deposited up to MHW
            total_mass_dep = new_marsh_height / (((1 - self._OCb[yr - 1]) / (self._rhos * 1000)) + (self._OCb[yr - 1] / (self._rhoo * 1000)))  # [g] Total mass to be deposited as new marsh in previous bay cell(s)
            min_mass_dep = total_mass_dep * (1 - self._OCb[yr - 1])  # [g] Mass of mineral sediment deposited in new marsh cell from marsh edge progradation
            org_mass_dep = total_mass_dep * self._OCb[yr - 1]  # [g] Mass of organic sediment deposited in new marsh cell from marsh edge progradation
            self._mineral_dep[yr, self._x_m: self._x_m + Dcells] += min_mass_dep
            self._organic_dep_alloch[yr, self._x_m: self._x_m + Dcells] += org_mass_dep
            Fm_min_prog = (min_mass_dep + Dcells) / 1000  # [kg/yr] Flux of mineral sediment from the bay from marsh edge progradation
            Fm_org_prog = (org_mass_dep + Dcells) / 1000  # [kg/yr] Flux of organic sediment from the bay from marsh edge progradation
        elif Dcells < 0:  # Marsh eroded
            # Account for negative deposition (i.e., erosion) in stratigraphic record: this is an ALTERATION/NEW ADDITION not included in original Matlab CoLT version
            for k in range(1, abs(Dcells) + 1):
                try:
                    boundyr_list = [i for i, x in enumerate(self._elevation[:yr, self._x_m - k]) if x < (self._msl[yr - 1] + self._amp - self._db)]
                except:
                    boundyr_list = []
                if len(boundyr_list) >= 1:
                    boundyr = boundyr_list[-1] + 1  # Most recent year where elevation of marsh edge has just risen above depth of erosion (i.e., bay bottom elevation)
                else:
                    boundyr = 0
                self._organic_dep_autoch[yr, self._x_m - k] -= np.sum(self._organic_dep_autoch[boundyr:, self._x_m - k])  # Subtract eroded mass from depositional record
                self._organic_dep_alloch[yr, self._x_m - k] -= np.sum(self._organic_dep_alloch[boundyr:, self._x_m - k])  # Subtract eroded mass from depositional record
                self._mineral_dep[yr, self._x_m - k] -= np.sum(self._mineral_dep[boundyr:, self._x_m - k])  # Subtract eroded mass from depositional record
            Fm_min_prog = 0
            Fm_org_prog = 0
        else:
            Fm_min_prog = 0
            Fm_org_prog = 0

        # Update bay depth
        self._elevation[yr, :self._x_m] = self._msl[yr] + self._amp - self._db  # All bay cells have the same depth

        # Add (or subtract) bay deposition: this is an ALTERATION/NEW ADDITION not included in original Matlab CoLT version
        db_change = (self._msl[yr] + self._amp - self._db) - (self._msl[yr - 1] + self._amp - self._Bay_depth[yr - 1])  # [m] Change in bay depth for this year
        total_mass_dep = db_change / (((1 - self._OCb[yr - 1]) / (self._rhos * 1000)) + (self._OCb[yr - 1] / (self._rhoo * 1000)))  # [g] Total mass to be deposited in bay cells
        min_mass_dep = total_mass_dep * (1 - self._OCb[yr - 1])  # [g] Mass of mineral sediment deposited in bay cells
        org_mass_dep = total_mass_dep * self._OCb[yr - 1]  # [g] Mass of organic sediment deposited in bay cells
        self._mineral_dep[yr, x_b_int: self._x_m] += min_mass_dep
        self._organic_dep_alloch[yr, x_b_int: self._x_m] += org_mass_dep

        # Mineral and organic marsh deposition
        (
            tempelevation,
            temporg_autoch,
            temporg_alloch,
            tempmin,
            self._Fm_min,
            self._Fm_org,
            tempbgb,
            accretion,
            tempagb,
        ) = evolvemarsh(
            tempelevation,
            self._msl[yr],
            self._C_e[yr],
            self._OCb[yr - 1],
            self._tr,
            self._numiterations,
            self._P,
            self._tidal_dt,
            self._ws,
            self._timestep,
            self._BMax,
            self._Dmin,
            self._Dmax,
            self._rhoo,
            self._rhos,
            plot=False
        )

        self._elevation[yr, self._x_m: self._x_f + 1] = tempelevation  # [m] Set new elevation to current year
        self._elevation[yr, self._x_f + 1: self._B] = self._elevation[yr - 1, self._x_f + 1: self._B]  # Forest elevation remains unchanged
        self._mineral_dep[yr, self._x_m: self._x_f + 1] += tempmin  # [g] Mineral sediment deposited in a given year
        self._organic_dep_autoch[yr, self._x_m: self._x_f + 1] = temporg_autoch  # [g] Belowground plant material deposited in a given year
        self._mortality[yr, self._x_m: self._x_f + 1] = temporg_autoch  # [g] Belowground plant material deposited in a given year, for keeping track of without decomposition
        self._organic_dep_alloch[yr, self._x_m: self._x_f + 1] = temporg_alloch  # [g] Allochthonous organic material deposited in a given year
        self._bgb_sum[yr] = np.sum(tempbgb)  # [g] Belowground biomass deposition summed across the marsh platform. Saved through time without decomposition for analysis

        self._Fm_min += Fm_min_prog  # [kg/yr] Add fluxes deposited at marsh edge to fluxes deposited on marsh platform
        self._Fm_org += Fm_org_prog  # [kg/yr] Add fluxes deposited at marsh edge to fluxes deposited on marsh platform

        try:
            self._x_f = max(self._x_m + 1, np.where(self._elevation[yr - 1, :] > self._msl[yr] + self._amp - self._Dmin + 0.03)[0][0])
        except IndexError:
            self._x_f = self._B
            self._drown_break = 1  # If x_f can't be found, barrier has drowned
            print("PyBMFT-C: Barrier has drowned.")
            self._endyear = yr
            return  # Exit program

        if self._forest_on:
            # Update forest soil organic matter
            spinlast25 = self._startyear - 25
            self._forestage += 1  # Age the forest
            for x in range(int(self._Forest_edge[yr - 1]), self._x_f + 1):
                if self._forestage < 80:
                    self._organic_dep_autoch[self._startyear - 25: self._startyear, x] = self._forestOM[:, yr - spinlast25] + self._B_rts[:, yr - spinlast25]
                else:
                    self._organic_dep_autoch[self._startyear - 25: self._startyear, x] = self._forestOM[:, 79] + self._B_rts[:, 79]
            for x in range(self._x_f, self._B):
                if self._forestage < 80:
                    self._organic_dep_autoch[self._startyear - 25: self._startyear, x] = self._forestOM[:, yr - spinlast25]
                    self._mineral_dep[self._startyear - 25: self._startyear, x] = self._forestMIN[:, yr - spinlast25]
                else:
                    self._organic_dep_autoch[self._startyear - 25: self._startyear, x] = self._forestOM[:, 79]
                    self._mineral_dep[self._startyear - 25: self._startyear, x] = self._forestMIN[:, 79]

            df = -self._msl[yr] + self._elevation[yr, self._x_f: self._B]

            self._organic_dep_autoch[yr, self._x_f: self._B] = self._f0 + self._fwet * np.exp(-self._fgrow * df)
            self._mineral_dep[yr, self._x_f: self._B] = self._forestMIN[0, 79]

            # Update forest aboveground biomass
            self._aboveground_forest[yr, self._x_f: self._B] = self._Bmax_forest / (1 + self._a * np.exp(-self._b * df))

        (
            compaction,
            tempFd,
            self._organic_dep_autoch,
        ) = decompose(
            self._x_m,
            self._x_f,
            yr,
            self._organic_dep_autoch,
            self._elevation,
            self._B,
            self._mui,
            self._mki,
            self._rhoo,
        )

        self._Fd[yr] = tempFd  # [kg] Flux of organic matter out of the marsh due to decomposition

        # Adjust marsh and forest elevation due to compaction from decomposition
        self._elevation[yr, self._x_m: self._B] -= compaction[self._x_m: self._B]
        self._OM_sum_au[yr, :len(self._elevation) + 1] = np.sum(self._organic_dep_autoch[:yr + 1, :])
        self._OM_sum_al[yr, :len(self._elevation) + 1] = np.sum(self._organic_dep_alloch[:yr + 1, :])

        F = 0
        while self._x_m < self._B and self._x_m < self._x_f:
            if self._organic_dep_autoch[yr, self._x_m] > 0 or (self._msl[yr] + self._amp - self._elevation[yr, self._x_m]) < self._Dmax:
                break
            else:  # Otherwise, the marsh has drowned, and will be eroded to form new bay
                F = 1
                self._edge_flood[yr] += 1  # Count that cell as a flooded cell
                self._bfo += 1  # Increase the bay fetch by one cell
                try:
                    boundyr_list = [i for i, x in enumerate(self._elevation[:yr, self._x_m]) if x < (self._msl[yr - 1] + self._amp - self._db)]
                except:
                    boundyr_list = []
                if len(boundyr_list) >= 1:
                    boundyr = boundyr_list[-1] + 1  # Most recent year where elevation of marsh edge has just risen above depth of erosion (i.e., bay bottom elevation)
                else:
                    boundyr = 0
                self._organic_dep_autoch[yr, self._x_m] -= np.sum(self._organic_dep_autoch[boundyr:, self._x_m])  # Subtract eroded mass from depositional record
                self._organic_dep_alloch[yr, self._x_m] -= np.sum(self._organic_dep_alloch[boundyr:, self._x_m])  # Subtract eroded mass from depositional record
                self._mineral_dep[yr, self._x_m] -= np.sum(self._mineral_dep[boundyr:, self._x_m])  # Subtract eroded mass from depositional record
                self._x_m += 1  # Update the new location of the marsh edge

        self._x_f = max(self._x_m + 1, self._x_f)  # "Forest" edge can't be less than or equal to marsh edge

        if F == 1:  # If flooding occurred, adjust marsh flux
            # Calculate the amount of organic and mineral sediment liberated from the flooded cells
            FF_org, FF_min = calcFE(self._bfo, self._fetch[yr - 1], self._elevation, yr, self._organic_dep_autoch, self._organic_dep_alloch, self._mineral_dep, self._rhou, self._x_b, self._msl, self._amp, self._db)
            # Adjust flux of mineral sediment to the marsh
            self._Fm_min -= FF_min
            # Adjust flux of organic sediment to the marsh
            self._Fm_org -= FF_org
            # Change the drowned marsh cell to z bay cell
            self._elevation[yr, :self._x_m] = self._elevation[yr, 0]

        self._fluxes[:, yr] = [
            Fe_min,
            Fe_org,
            self._Fm_min,
            self._Fm_org,
            Fc_min,
            Fc_org,
            Fb_min,
            Fb_org,
        ]

        # Update inputs for marsh edge
        self._Marsh_edge[yr] = self._x_m
        self._Forest_edge[yr] = self._x_f
        self._Bay_depth[yr] = self._db
        self._BaySedDensity[self._time_index] = self._rhob

        if 0 < self._x_m < self._B:
            self._dmo = self._msl[yr] + self._amp - self._elevation[yr, self._x_m]
            self._Edge_ht[yr] = self._dmo
        elif int(self._bfo) <= 10:  # Condition for if the marsh has expanded to fill the basin
            self._drown_break = 1
            print("PyBMFT-C: Marsh has completely filled the basin")
            self._endyear = yr
            return  # Exit program
        elif self._x_m <= 10:  # Another condition for if the marsh has expanded to fill the basin
            self._drown_break = 1
            print("PyBMFT-C: Marsh has expanded to fill the basin.")
            self._endyear = yr
            return  # Exit program
        elif self._x_m >= len(self._elevation[0, :]) - 10:  # Condition for if the marsh has eroded completely away
            self._drown_break = 1
            print("PyBMFT-C: Marsh has retreated. Basin is completely flooded.")
            self._endyear = yr
            return  # Exit program
        elif self._db < 0.2:  # Condition for if the bay gets very shallow. Should this number be calculated within the code?
            self._drown_break = 1
            print("PyBMFT-C: Bay has filled in to form marsh.")
            self._endyear = yr
            return  # Exit program

        self._fetch[yr] = self._bfo  # Save change in bay fetch through time

        self._Fc_ODE = []
        self._C_e_ODE = []

        # Increase time
        self._time_index += 1

        # TIME STEP COMPLETE
        # ==========================================================================================================================================================================

    @property
    def time_index(self):
        return self._time_index

    @property
    def dur(self):
        return self._dur

    @property
    def organic_dep_autoch(self):
        return self._organic_dep_autoch

    @property
    def x_m(self):
        return self._x_m

    @property
    def x_f(self):
        return self._x_f

    @property
    def organic_dep_alloch(self):
        return self._organic_dep_alloch

    @property
    def endyear(self):
        return self._endyear

    @property
    def mineral_dep(self):
        return self._mineral_dep

    @property
    def elevation(self):
        return self._elevation

    @property
    def B(self):
        return self._B

    @property
    def bfo(self):
        return self._bfo

    @property
    def startyear(self):
        return self._startyear

    @property
    def fetch(self):
        return self._fetch

    @property
    def Bay_depth(self):
        return self._Bay_depth

    @property
    def RSLRi(self):
        return self._RSLRi

    @property
    def db(self):
        return self._db

    @property
    def x_b(self):
        return self._x_b

    @property
    def msl(self):
        return self._msl

    @property
    def amp(self):
        return self._amp

    @property
    def Dmin(self):
        return self._Dmin

    @property
    def Marsh_edge(self):
        return self._Marsh_edge

    @property
    def tcr(self):
        return self._tcr

    @property
    def slope(self):
        return self._slope

    @property
    def Co(self):
        return self._Co

    @property
    def mwo(self):
        return self._mwo

    @property
    def wind(self):
        return self._wind

    @property
    def Forest_edge(self):
        return self._Forest_edge

    @property
    def rhos(self):
        return self._rhos

    @property
    def dmo(self):
        return self._dmo

    @property
    def Edge_ht(self):
        return self._Edge_ht

    @property
    def drown_break(self):
        return self._drown_break

    @property
    def forest_width_initial_fixed(self):
        return self._forest_width_initial_fixed

    @property
    def forest_width_initial(self):
        return self._forest_width_initial

    @property
    def Fow_min(self):
        return self._Fow_min

    @property
    def OCb(self):
        return self._OCb

    @property
    def C_e(self):
        return self._C_e

    @property
    def fluxes(self):
        return self._fluxes

    @property
    def BaySedDensity(self):
        return self._BaySedDensity

    @property
    def rhomt(self):
        return self._rhomt

    @property
    def massmt(self):
        return self._massmt

    @property
    def rhob(self):
        return self._rhob

    @property
    def name(self):
        return self._name

    @property
    def Dmax(self):
        return self._Dmax
