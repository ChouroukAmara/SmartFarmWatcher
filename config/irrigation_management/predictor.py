import joblib
import pandas as pd
from pathlib import Path

MODEL_DIR = Path(__file__).parent / "ml_model"

_bundle = None

# ── ML categories the model was actually trained on ────────────────
# Used only to validate crop_type before building the one-hot vector.
# The salinity threshold (ece) is NOT looked up here anymore — it comes
# from CropCoefficient.ece_threshold in the DB (see Project model).
TRAINED_CROP_TYPES = ['tomato', 'potato']

# ── Base deficit coefficient per FAO-56 growth stage (RDI logic) ──────
STAGE_DEFICIT_BASE = {
    'initial'    : 0.60,
    'development': 0.80,
    'mid_season' : 0.95,
    'end_cycle'  : 0.75,
}

# Maps Project.current_stage's display strings to the model's stage keys
STAGE_DISPLAY_TO_KEY = {
    'Initial'        : 'initial',
    'Development'     : 'development',
    'Mid-season'      : 'mid_season',
    'End of season'   : 'end_cycle',
}

# ── Soil water-holding parameters (FAO-56 Table 19) ────────────────────
# Used ONLY to build the reference value below, NOT fed to the ML model
# (soil_moisture_pct / soil_type are given directly to the model instead).
SOIL_PARAMS = {
    'sandy': {'theta_fc': 10.0, 'theta_wp': 2.0},
    'loamy': {'theta_fc': 22.5, 'theta_wp': 10.0},
    'clay' : {'theta_fc': 37.5, 'theta_wp': 22.5},
}


def _load():
    global _bundle
    if _bundle is None:
        _bundle = joblib.load(MODEL_DIR / "irrigation_bundle.pkl")


def map_stage(display_stage):
    """
    Converts Project.current_stage's display string ('Mid-season', ...)
    into the model's expected stage key ('mid_season', ...).
    Raises ValueError if unrecognized.
    """
    if display_stage not in STAGE_DISPLAY_TO_KEY:
        raise ValueError(
            f"Unrecognized stage '{display_stage}'. "
            f"Expected one of {list(STAGE_DISPLAY_TO_KEY)}"
        )
    return STAGE_DISPLAY_TO_KEY[display_stage]


def predict_recommended_irrigation(et0_mm, kc, soil_moisture_pct, soil_type,
                                    ecw, ece, crop_type, crop_stage,
                                    rainfall_mm=0.0):
    """
    Predicts recommended_irrigation_mm (deficit irrigation under saline
    water conditions) with the best trained model (Gradient Boosting).

    Changes vs the previous predict_btotaux():
      - Target is recommended_irrigation_mm, not Btotaux (FAO-29 leaching
        logic removed entirely: no FL, no BL, no Btotaux_FAO).
      - EG / irrigation_type removed: no efficiency division anywhere.
      - Ks is NOT a model feature anymore. soil_moisture_pct and soil_type
        are passed directly to the model (one-hot for soil_type) so it
        learns the stress relationship itself. Ks is still computed here,
        but only to build a reference value for sanity-checking the
        ML prediction, exactly like during training/validation.
      - crop_type is restricted to 'tomato' / 'potato' only (the two
        categories the model was trained on).
      - crop_stage is now a required input (FAO-56 4-stage model). Use
        map_stage(project.current_stage) to convert from the Project
        model's display strings ('Mid-season', ...) to the expected
        key ('mid_season', ...).
      - ece is now passed in explicitly (comes from CropCoefficient.
        ece_threshold in the DB) rather than looked up from a hardcoded
        dict, since the DB already stores this per crop.

    Parameters
    ----------
    et0_mm : float              Reference evapotranspiration (mm/day)
    kc     : float               Crop coefficient (e.g. project.current_kc)
    soil_moisture_pct : float    Raw soil moisture reading (%)
    soil_type : str              'sandy' | 'loamy' | 'clay'
    ecw : float                   Irrigation water salinity (dS/m)
    ece : float                   Crop salinity tolerance threshold (dS/m),
                                   from CropCoefficient.ece_threshold
    crop_type : str               'tomato' | 'potato' (lowercase!)
    crop_stage : str              'initial' | 'development' | 'mid_season' | 'end_cycle'
    rainfall_mm : float           Daily rainfall (mm), default 0.0

    Returns
    -------
    dict with ML prediction, reference value, and diagnostic fields.
    """
    _load()

    model      = _bundle['model']
    scaler     = _bundle['scaler']
    features   = _bundle['features']
    use_scaled = _bundle['use_scaled']
    soil_types  = _bundle.get('soil_types',  list(SOIL_PARAMS))
    crop_stages = _bundle.get('crop_stages', list(STAGE_DEFICIT_BASE))
    crop_types  = _bundle.get('crop_types',  TRAINED_CROP_TYPES)

    crop_type = crop_type.lower().strip()
    if crop_type not in crop_types:
        raise ValueError(
            f"This model was only trained on {crop_types}. "
            f"Got crop_type='{crop_type}'. Add this crop to the training "
            f"dataset and retrain before using it here."
        )
    if crop_stage not in STAGE_DEFICIT_BASE:
        raise ValueError(f"Invalid crop_stage '{crop_stage}'. Expected one of {crop_stages}")
    if soil_type not in SOIL_PARAMS:
        raise ValueError(f"Invalid soil_type '{soil_type}'. Expected one of {soil_types}")

    # ── Ks (reference-formula use ONLY, not a model feature) ──────────
    params   = SOIL_PARAMS[soil_type]
    theta_fc, theta_wp = params['theta_fc'], params['theta_wp']
    theta_t  = theta_wp + 0.5 * (theta_fc - theta_wp)
    if soil_moisture_pct >= theta_t:
        ks = 1.0
    elif soil_moisture_pct <= theta_wp:
        ks = 0.0
    else:
        ks = (soil_moisture_pct - theta_wp) / (theta_t - theta_wp)

    etc_mm  = et0_mm * kc
    etc_adj = etc_mm * ks

    # ── Build X_input with the exact one-hot layout used in training ──
    row = {feat: 0 for feat in features}
    row['etc_mm']        = etc_mm
    row['soil_moisture'] = soil_moisture_pct
    row['ecw']            = ecw
    row['ece']            = ece
    row['rainfall_mm']    = rainfall_mm

    soil_col  = f'soil_type_{soil_type}'
    stage_col = f'crop_stage_{crop_stage}'
    type_col  = f'crop_type_{crop_type}'
    if soil_col  in row: row[soil_col]  = 1
    if stage_col in row: row[stage_col] = 1
    if type_col  in row: row[type_col]  = 1

    X_input = pd.DataFrame([row])[features]
    if use_scaled:
        X_input = scaler.transform(X_input)

    ml_prediction = float(model.predict(X_input)[0])
    ml_prediction = max(ml_prediction, 0.0)

    # ── Deficit-based reference (same formula as dataset generation) ──
    pu = 0.0
    if rainfall_mm > 0:
        pu = max(1.25 * (rainfall_mm ** 0.824)
                 - 2.93 * (10 ** (0.000955 * etc_adj)), 0)
        pu = min(pu, etc_adj)

    bnet = max(etc_adj - pu, 0)   # NO EG division

    salinity_ratio = ecw / ece if ece else 0.0
    excess = max(salinity_ratio - 0.5, 0.0)
    salinity_adjustment = min(excess * 0.35, 0.25)
    deficit_coeff = min(max(STAGE_DEFICIT_BASE[crop_stage] + salinity_adjustment, 0.30), 1.00)

    reference_recommendation = max(bnet * deficit_coeff, 0)

    return {
        'ETc_mm'                   : round(etc_mm, 3),
        'ETc_adj_mm'               : round(etc_adj, 3),
        'Ks_reference_only'        : round(ks, 3),
        'Salinity_Ratio'           : round(salinity_ratio, 3),
        'Deficit_Coefficient'      : round(deficit_coeff, 3),
        'Reference_recommendation' : round(reference_recommendation, 3),
        'Recommended_Irrigation_mm': round(ml_prediction, 3),   # <- main field for the dashboard
        'Difference'               : round(abs(ml_prediction - reference_recommendation), 3),
        'Alert'                    : abs(ml_prediction - reference_recommendation) > 1.0,
    }