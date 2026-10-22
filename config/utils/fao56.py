import requests
from datetime import date
from django.conf import settings
import pyeto


class FAO56Calculator:
    OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"

    def __init__(self, latitude: float, longitude: float):
        self.latitude         = latitude
        self.longitude        = longitude
        self._weather_cache   = None
        self._elevation_cache = None

    # ─────────────────────────────────────────
    # Fetch weather
    # ─────────────────────────────────────────
    def fetch_weather(self) -> dict:
        if self._weather_cache:
            return self._weather_cache
        try:
            resp = requests.get(self.OPENWEATHER_URL, params={
                "lat":   self.latitude,
                "lon":   self.longitude,
                "appid": settings.OPENWEATHER_API_KEY,
                "units": "metric",
            }, timeout=5)
            resp.raise_for_status()
            d = resp.json()

            rain        = d.get("rain", {})
            rainfall_mm = rain.get("3h", None) or rain.get("1h", 0.0)

            self._weather_cache = {
                "wind_speed":  d.get("wind",   {}).get("speed", 2.0),
                "clouds_pct":  d.get("clouds", {}).get("all",   50),
                "rainfall_mm": rainfall_mm,
                "T_min":       d.get("main",   {}).get("temp_min", None),
                "T_max":       d.get("main",   {}).get("temp_max", None),
            }
        except Exception as e:
            print(f"[FAO56] OpenWeatherMap error: {e}")
            self._weather_cache = {
                "wind_speed": 2.0, "clouds_pct": 50,
                "rainfall_mm": 0.0, "T_min": None, "T_max": None,
            }
        return self._weather_cache

    # ─────────────────────────────────────────
    # Fetch elevation
    # ─────────────────────────────────────────
    def fetch_elevation(self) -> float:
        if self._elevation_cache is not None:
            return self._elevation_cache
        try:
            resp = requests.get(
                "https://api.open-elevation.com/api/v1/lookup",
                params={"locations": f"{self.latitude},{self.longitude}"},
                timeout=5,
            )
            resp.raise_for_status()
            self._elevation_cache = resp.json()["results"][0]["elevation"]
        except Exception as e:
            print(f"[FAO56] Elevation error: {e}")
            self._elevation_cache = 50.0
        return self._elevation_cache

    # ─────────────────────────────────────────
    # Calcul ET0
    # ─────────────────────────────────────────
    def compute_et0(self, T_mean: float, RH_mean: float, P_hpa: float = 1013.0) -> dict:
        weather    = self.fetch_weather()
        u10        = weather["wind_speed"]
        clouds_pct = weather["clouds_pct"]
        T_min      = weather["T_min"] if weather["T_min"] is not None else T_mean - 3
        T_max      = weather["T_max"] if weather["T_max"] is not None else T_mean + 3

        J       = date.today().timetuple().tm_yday
        lat_rad = pyeto.deg2rad(self.latitude)
        u2      = pyeto.wind_speed_2m(u10, z=10)

        svp_min = pyeto.svp_from_t(T_min)
        svp_max = pyeto.svp_from_t(T_max)
        #svp     = pyeto.mean_svp(svp_min, svp_max)
        svp = pyeto.mean_svp(T_min, T_max)
        avp     = round(svp * RH_mean / 100.0, 4)

        sol_dec  = pyeto.sol_dec(J)
        sha      = pyeto.sunset_hour_angle(lat_rad, sol_dec)
        ird      = pyeto.inv_rel_dist_earth_sun(J)
        Ra       = pyeto.et_rad(lat_rad, sol_dec, sha, ird)
        dl_hours = pyeto.daylight_hours(sha)
        Rs       = pyeto.sol_rad_from_sun_hours(
            daylight_hours=dl_hours,
            sunshine_hours=dl_hours * (1.0 - clouds_pct / 100.0),
            et_rad=Ra,
        )

        altitude = self.fetch_elevation()
        Rso      = pyeto.cs_rad(altitude, Ra)

        Rns = pyeto.net_in_sol_rad(Rs)
        Rnl = pyeto.net_out_lw_rad(
            tmin=pyeto.celsius2kelvin(T_min),
            tmax=pyeto.celsius2kelvin(T_max),
            sol_rad=Rs,
            cs_rad=Rso,
            avp=avp,
        )
        Rn    = pyeto.net_rad(Rns, Rnl)
        gamma = pyeto.psy_const(P_hpa / 10.0)
        delta = pyeto.delta_svp(T_mean)

        et0 = pyeto.fao56_penman_monteith(
            net_rad=Rn,
            t=pyeto.celsius2kelvin(T_mean),
            ws=u2,
            svp=svp,
            avp=avp,
            delta_svp=delta,
            psy=gamma,
            shf=0,
        )

        print(f"[DEBUG] altitude={altitude:.1f}m")
        print(f"[DEBUG] Rn={Rn:.4f} Ra={Ra:.4f} Rs={Rs:.4f} Rso={Rso:.4f}")
        print(f"[DEBUG] u2={u2:.4f} svp={svp:.4f} avp={avp:.4f}")
        print(f"[DEBUG] delta={delta:.4f} gamma={gamma:.4f}")
        print(f"[DEBUG] et0_raw={et0:.4f}")

        et0 = round(max(et0, 0), 3)

        return {
            "et0_mm":     et0,
            "Ra_MJ":      round(Ra,        3),
            "Rs_MJ":      round(Rs,        3),
            "Rn_MJ":      round(Rn,        3),
            "svp_kPa":    round(svp,       4),
            "avp_kPa":    round(avp,       4),
            "VPD_kPa":    round(svp - avp, 4),
            "u2_ms":      round(u2,        2),
            "clouds_pct": clouds_pct,
            "T_min":      T_min,
            "T_max":      T_max,
            "altitude_m": round(altitude,  1),
        }

    # ─────────────────────────────────────────
    # Calcul ETc = ET0 × Kc
    # ─────────────────────────────────────────
    def compute_etc(self, et0: float, kc: float) -> float:
        return round(et0 * kc, 3)

    # ─────────────────────────────────────────
    # Précipitation utile Pu — équation 3.13
    # ─────────────────────────────────────────
    def compute_pu(self, rainfall_mm: float, etc_mm: float) -> float:
        """
        Pu = 1.25 × Pt^0.824 − 2.93 × 10^(0.000955 × ETc)
        Si Pt = 0 → Pu = 0
        """
        if rainfall_mm <= 0:
            return 0.0
        pu = 1.25 * (rainfall_mm ** 0.824) - 2.93 * (10 ** (0.000955 * etc_mm))
        return round(max(pu, 0.0), 3)

    # ─────────────────────────────────────────
    # Efficience globale EG — équation 3.9
    # ─────────────────────────────────────────
    @staticmethod
    def compute_efficiency(irrigation_type: str = "drip") -> dict:
        """
        EG = Ep × Er  avec Er = 0.9
        surface   : Ep = 0.525 → EG = 0.47
        sprinkler : Ep = 0.675 → EG = 0.61
        drip      : Ep = 0.900 → EG = 0.81
        """
        EP_MAP = {
            "surface":   {"ep_min": 0.40, "ep_max": 0.65,
                          "ep_mid": 0.525, "label": "Irrigation de surface"},
            "sprinkler": {"ep_min": 0.60, "ep_max": 0.75,
                          "ep_mid": 0.675, "label": "Aspersion"},
            "drip":      {"ep_min": 0.85, "ep_max": 0.95,
                          "ep_mid": 0.90,  "label": "Micro-irrigation"},
        }
        ER     = 0.9
        params = EP_MAP.get(irrigation_type, EP_MAP["drip"])
        return {
            "irrigation_type": irrigation_type,
            "label":           params["label"],
            "Er":              ER,
            "Ep":              params["ep_mid"],
            "EG":              round(params["ep_mid"] * ER, 3),
            "EG_min":          round(params["ep_min"] * ER, 3),
            "EG_max":          round(params["ep_max"] * ER, 3),
        }

    # ─────────────────────────────────────────
    # Lessivage FL et BL — équations 3.10 / 3.11
    # ─────────────────────────────────────────
    @staticmethod
    def compute_leaching(ecw: float, ece: float, b_bruts: float) -> dict:
        """
        ECw : salinité eau irrigation (dS/m) — capteur salinity ms/cm = dS/m
        ECe : seuil tolérance culture FAO  (dS/m) — CropCoefficient.ece_threshold
        FL  = ECw / (5 × ECe − ECw)
        BL  = FL × Bbruts
        """
        if ecw <= 0:
            return {
                "FL": 0.0, "BL": 0.0,
                "salinity_risk": "none",
                "salinity_alert": "",
            }

        denominator = (5.0 * ece) - ecw
        if denominator <= 0:
            fl = 1.0
        else:
            fl = ecw / denominator

        fl = round(min(max(fl, 0.0), 1.0), 4)
        bl = round(fl * b_bruts, 3)

        # Niveau de risque
        if ecw <= ece:
            risk  = "none"
            alert = ""
        elif ecw <= ece * 2:
            risk  = "low"
            alert = f"⚠️ ECw ({ecw} dS/m) dépasse le seuil FAO ({ece} dS/m)."
        elif ecw <= ece * 3:
            risk  = "medium"
            alert = (f"⚠️ Salinité élevée : ECw={ecw} dS/m — "
                     f"seuil culture={ece} dS/m. Lessivage {bl} mm requis.")
        else:
            risk  = "high"
            alert = (f"🚨 Salinité critique : ECw={ecw} dS/m — "
                     f"seuil={ece} dS/m. Lessivage urgent {bl} mm !")

        return {
            "FL":             fl,
            "BL":             bl,
            "salinity_risk":  risk,
            "salinity_alert": alert,
        }

    # ─────────────────────────────────────────
    # Stress hydrique sol — depuis soil_moisture
    # ─────────────────────────────────────────
    @staticmethod
    def compute_soil_stress(
            soil_moisture_pct: float,
            soil_type: str = "loamy") -> dict:
        """
        Valeurs θCC et θPF selon Tableau 3.1
        Retourne coefficient de stress Ks (0→1)
        Ks=1 : pas de stress  /  Ks=0 : stress total
        """
        SOIL_PARAMS = {
            "sandy": {"theta_cc": 10.0,  "theta_pf": 2.0},
            "loamy": {"theta_cc": 22.5,  "theta_pf": 10.0},
            "clay":  {"theta_cc": 37.5,  "theta_pf": 22.5},
        }
        params   = SOIL_PARAMS.get(soil_type, SOIL_PARAMS["loamy"])
        theta_cc = params["theta_cc"]
        theta_pf = params["theta_pf"]
        theta_pc = theta_pf + 0.5 * (theta_cc - theta_pf)

        if soil_moisture_pct >= theta_pc:
            ks     = 1.0
            status = "optimal"
        elif soil_moisture_pct <= theta_pf:
            ks     = 0.0
            status = "critical"
        else:
            ks     = (soil_moisture_pct - theta_pf) / (theta_pc - theta_pf)
            status = "stress"

        return {
            "Ks":               round(ks, 3),
            "theta_cc":         theta_cc,
            "theta_pf":         theta_pf,
            "theta_pc":         round(theta_pc, 1),
            "soil_moisture_pct": soil_moisture_pct,
            "soil_status":      status,
        }

    # ─────────────────────────────────────────
    # Conseil irrigation complet — équation 3.12
    # ─────────────────────────────────────────
    def compute_irrigation_advice(
        self,
        etc_mm:            float,
        et0_mm:            float,
        kc:                float,
        stage:             str,
        ecw:               float = 0.0,
        ece:               float = 2.5,
        soil_moisture_pct: float = None,
        soil_type:         str   = "loamy",
        irrigation_type:   str   = "drip",
        rainfall_mm:       float = None,
    ) -> dict:

        # 1 — Pluie brute depuis OpenWeatherMap
        if rainfall_mm is None:
            rainfall_mm = self.fetch_weather()["rainfall_mm"]

        # 2 — Précipitation utile Pu (éq. 3.13)
        pu = self.compute_pu(rainfall_mm, etc_mm)

        # 3 — Efficience globale EG (éq. 3.9)
        efficiency_info = self.compute_efficiency(irrigation_type)
        eg              = efficiency_info["EG"]

        # 4 — Besoins nets et bruts sans lessivage (éq. 3.8)
        b_nets  = round(max(etc_mm - pu, 0.0), 3)
        b_bruts = round(b_nets / eg, 3) if eg > 0 else b_nets

        # 5 — Lessivage FL / BL (éqs. 3.10 / 3.11)
        leaching = self.compute_leaching(ecw, ece, b_bruts)
        bl       = leaching["BL"]

        # 6 — Besoins totaux avec salinité (éq. 3.12)
        # Btotaux = (ETc - Pu + BL) / EG
        #b_totaux = round((etc_mm - pu + bl) / eg, 3) if eg > 0 else 0.0
        # ✅ LIGNE 6 CORRIGÉE — Btotaux = Bbruts + BL
        b_totaux = round(b_bruts + bl, 3)
        b_totaux = max(b_totaux, 0.0)

        # 7 — Stress hydrique sol (depuis capteur soil_moisture)
        soil_stress = None
        if soil_moisture_pct is not None:
            soil_stress = self.compute_soil_stress(soil_moisture_pct, soil_type)
            # Ks affiché dans le dashboard uniquement, ne modifie pas Btotaux

        # 8 — Niveau de conseil
        if b_totaux == 0:
            level, color = "none",   "#2e7d32"
        elif b_totaux < 2:
            level, color = "low",    "#1565c0"
        elif b_totaux < 5:
            level, color = "medium", "#e65100"
        else:
            level, color = "high",   "#b71c1c"

        # 9 — Message final avec alerte salinité
        sal_alert = leaching["salinity_alert"]
        msgs = {
            "none":   f"✅ Pas d'irrigation nécessaire. {sal_alert}".strip(),
            "low":    f"💧 Irrigation légère : ~{b_totaux} mm. {sal_alert}".strip(),
            "medium": f"💧💧 Irrigation modérée : ~{b_totaux} mm. {sal_alert}".strip(),
            "high":   f"🚨 Irrigation urgente : ~{b_totaux} mm. {sal_alert}".strip(),
        }

        return {
            # Entrées
            "kc":               kc,
            "stage":            stage,
            # Efficience
            "irrigation_type":  irrigation_type,
            "irrigation_label": efficiency_info["label"],
            "Ep":               efficiency_info["Ep"],
            "Er":               efficiency_info["Er"],
            "EG":               eg,
            "EG_min":           efficiency_info["EG_min"],
            "EG_max":           efficiency_info["EG_max"],
            # ET
            "et0_mm":           et0_mm,
            "etc_mm":           etc_mm,
            # Pluie
            "rainfall_mm":      round(rainfall_mm, 3),
            "pu_mm":            pu,
            # Salinité
            "ecw_dSm":          round(ecw, 3),
            "ece_dSm":          ece,
            "FL":               leaching["FL"],
            "BL_mm":            bl,
            "salinity_risk":    leaching["salinity_risk"],
            "salinity_alert":   leaching["salinity_alert"],
            # Besoins
            "b_nets_mm":        b_nets,
            "b_bruts_mm":       b_bruts,
            "b_totaux_mm":      b_totaux,
            # Sol
            "soil_stress":      soil_stress,
            # Conseil
            "advice_level":     level,
            "advice_color":     color,
            "message":          msgs[level],
            # Rétrocompatibilité IrrigationLog existant
            "net_need_mm":      b_nets,
            "gross_need_mm":    b_bruts,
            "efficiency":       eg,
        }