import os
import csv
import json
import secrets
from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache, wraps
from io import StringIO
from shutil import disk_usage
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, render_template, request, redirect, url_for, session, Response
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from models import db, User, Admin, Scan, AuditLog, SystemConfig, Notification, Feedback, AgronomicLog, CvScanUpload


ORIGINAL_ENV_KEYS = set(os.environ.keys())


def load_env_file(path, override=False):
    """Load KEY=VALUE pairs from an env file.

    Shell-exported env vars always win; local files can optionally override earlier file values.
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if value and len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                if key in ORIGINAL_ENV_KEYS:
                    continue
                if override or key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


load_env_file(".env.example", override=False)
load_env_file(".env.local", override=True)
load_env_file(".env", override=True)

DEFAULT_VARIETY_WEIGHTS = {
    "VMC 84-524": {
        "rssi": -0.45,
        "weeding": 0.35,
        "fertilizer": 0.25,
        "ratoon": -0.12,
        "plowing": 0.08,
    },
    "VMC 84-947": {
        "rssi": -0.45,
        "weeding": 0.28,
        "fertilizer": 0.18,
        "ratoon": -0.05,
        "plowing": 0.05,
    },
    "MAURITIO RC888": {
        "rssi": -0.55,
        "weeding": 0.28,
        "fertilizer": 0.18,
        "ratoon": -0.12,
        "plowing": 0.08,
    },
}

CV_MATURITY_BASELINE_WEIGHTS = {
    "VMC 84-524": {
        "NOT_MATURE": -0.20,
        "MATURE": 0.00,
        "OVER_MATURE": -0.15,
    },
    "VMC 84-947": {
        "NOT_MATURE": -0.18,
        "MATURE": 0.00,
        "OVER_MATURE": -0.22,
    },
    "MAURITIO RC888": {
        "NOT_MATURE": -0.22,
        "MATURE": 0.00,
        "OVER_MATURE": -0.18,
    },
}

SRA_BASELINE_LKG_TC = {
    "VMC 84-524": {1: 2.04, 2: 1.94, 3: 2.40},
    "VMC 84-947": {1: 1.86, 2: 2.14, 3: 2.21},
    "MAURITIO RC888": {1: 2.22, 2: 2.26, 3: 1.99},
}

SRA_BASELINE_TC_HA = {
    "VMC 84-524": {1: 239.0, 2: 165.0, 3: 134.0},
    "VMC 84-947": {1: 285.0, 2: 256.0, 3: 179.0},
    "MAURITIO RC888": {1: 273.06, 2: 250.86, 3: 143.28},
}

CROP_STAGE_LABELS = {
    1: "New Plant (1)",
    2: "1st ratoon (2nd)",
    3: "2nd ratoon (3rd)",
}

AGRONOMIC_KEYS = ["rssi", "weeding", "fertilizer", "ratoon", "plowing"]
TRAINING_NUMERIC_FEATURES = ["hectares", "rssi", "weeding", "fertilizer", "ratoon", "plowing"]
TRAINING_CATEGORICAL_FEATURES = ["variety"]
TRAINING_FEATURE_COLUMNS = TRAINING_CATEGORICAL_FEATURES + TRAINING_NUMERIC_FEATURES
TRAINING_TARGET_COLUMNS = ["predicted_lkg_tc", "predicted_tc_ha", "predicted_lkg"]
TRAINING_TARGET_LABELS = {
    "predicted_lkg_tc": "Predicted LKG/TC",
    "predicted_tc_ha": "Predicted TC/HA",
    "predicted_lkg": "Predicted LKG",
}
TRAINING_REQUIRED_COLUMNS = {
    "variety",
    "hectares",
    "plowing_count",
    "weeding_count",
    "fertilizer_count",
    "ratoon_stage",
    "rssi_infected",
    "predicted_lkg_tc",
    "predicted_tc_ha",
    "predicted_lkg",
}
DEFAULT_DATASET_PATH = "data/agronomic_training.csv"
MIN_DATASET_ROWS = max(8, int(os.getenv("AGRONOMIC_MODEL_MIN_ROWS", "12")))
AGRONOMIC_TRAINING_MODE = os.getenv("AGRONOMIC_TRAINING_MODE", "legacy_weighted").strip().lower()
LEGACY_TRAIN_HECTARES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 5.5]
LEGACY_TRAIN_LEVELS = [1.0, 2.0, 3.0]
VARIETY_ALIASES = {
    "Mauritius RC888": "MAURITIO RC888",
    "MAURITIO RC888": "MAURITIO RC888",
}
CV_VARIETY_ALIASES = {
    "524": "VMC 84-524",
    "VMC 84-524": "VMC 84-524",
    "847": "VMC 84-947",
    "VMC 84-947": "VMC 84-947",
    "MAURITIO": "MAURITIO RC888",
    "MAURITIO RC888": "MAURITIO RC888",
}

FERTILIZER_TIMING_GUIDE = {
    "VMC 84-524": {
        1: "1-Time: Apply all fertilizer at planting or 1 month after planting.",
        2: "2-Time: Half at ~45 days, half at ~3 months before canopy closure. Alternative: first dose 3-4 days after planting, second at 3 months.",
        3: "3-Time: First at planting, second after 1-2 months, third at 3-4 months.",
    },
    "VMC 84-947": {
        1: "1-Time: Apply full fertilizer at planting or right after ratoon starts based on soil test.",
        2: "2-Time: Split into two doses at ~1.5 months and ~3 months before canopy closure.",
        3: "3-Time: Split N and K into 3 equal doses at 30, 60, and 90 days after planting.",
    },
    "MAURITIO RC888": {
        1: "1-Time: Apply full N-P-K at planting.",
        2: "2-Time: Basal dose at planting, then top dress around 3 months.",
        3: "3-Time: Split nitrogen into 3 doses within first 3-4 months, or at 30, 60, and 90 days after planting.",
    },
}

WEEDING_GUIDE = {
    "VMC 84-524": {
        1: "1-Time: Not recommended. Weed pressure can become too high, and glyphosate should be avoided during germination and tillering due to crop sensitivity.",
        2: "2-Time: Acceptable when combined with chemical weeding. Use 2,4-D or Diuron early for safe control.",
        3: "3-Time: Best practice. Combine manual weeding with selective herbicides (2,4-D or Diuron) to keep the field clean and reduce lodging risk.",
    },
    "VMC 84-947": {
        1: "1-Time: Not recommended. A single weeding is not enough for this fast-growing variety.",
        2: "2-Time: Better, but still limited. Weed competition may reduce internode elongation and ratoon strength.",
        3: "3-Time: Strongly recommended. Use pre-emergence spraying plus three manual weedings at 25, 45, and 65 days after planting.",
    },
    "MAURITIO RC888": {
        1: "1-Time: Risky. Weed stress can weaken plant defense and increase leaf scald vulnerability.",
        2: "2-Time: Possible, but reduced weeding can increase disease risk.",
        3: "3-Time: Best practice. Perform manual weeding at 25, 45, and 65 days after planting to reduce stress and disease outbreaks.",
    },
}

PLOWING_GUIDE = {
    "VMC 84-524": {
        1: "1-Time: Only suitable for shallow soils with hardpan underneath; excess plowing may bring up infertile soil.",
        2: "2-Time: Acceptable when spaced 1-2 weeks apart; first pass encourages weed seed sprouting, second pass suppresses weeds.",
        3: "3-Time: Highly recommended with deep passes (8-12 inches or ~50-60 cm with heavy tractors) to improve rooting and lodging resistance.",
    },
    "VMC 84-947": {
        1: "1-Time (Plant Crop): Not recommended due to poor soil preparation and reduced ratooning lifespan.",
        2: "2-Time (Plant Crop): Recommended for new planting to prepare soil thoroughly for multiple ratoon cycles.",
        3: "3-Time (Plant Crop): Best practice for deep soil preparation and stronger long-term ratoon performance.",
    },
    "MAURITIO RC888": {
        1: "1-Time: Risky; can leave stubble and weeds near the surface, increasing disease pressure and weakening crop vigor.",
        2: "2-Time: Acceptable if followed by thorough harrowing to clean and condition the soil.",
        3: "3-Time: Strongly recommended to bury residues/weeds and improve drainage against waterlogging stress.",
    },
}

DEFAULT_RECOMMENDATIONS = [
    {
        "icon": "calendar-outline",
        "title": "Schedule harvest for ready plot",
        "meta": "Prioritize fields with high maturity this week.",
        "tag": "Priority",
        "tag_class": "success",
        "category": "General",
    },
    {
        "icon": "color-wand-outline",
        "title": "Apply nutrient mix",
        "meta": "Support sucrose build-up before harvest.",
        "tag": "Soon",
        "tag_class": "warning",
        "category": "General",
    },
    {
        "icon": "trail-sign-outline",
        "title": "Prepare transport route",
        "meta": "Finalize hauling logistics before cutting day.",
        "tag": "Plan",
        "tag_class": "",
        "category": "General",
    },
]

DEFAULT_SCAN_PREDICT_ENDPOINT = "http://34.81.143.245:8000/predict"
DEFAULT_SCAN_PREDICT_TOP_K = 3
DEFAULT_SCAN_PREDICT_TIMEOUT_SECONDS = 30
CV_UPLOAD_RELATIVE_DIR = os.path.join("uploads", "cv_scans")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('VISCANE_SECRET_KEY', 'change-this-key')

database_url = (
    os.getenv('SQLALCHEMY_DATABASE_URI')
    or os.getenv('DATABASE_URL')
    or os.getenv('DATABASE_FALLBACK_URL')
    or 'postgresql://user:password@localhost:5433/viscane_db'
)
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
if not database_url.startswith('postgresql://'):
    raise RuntimeError('This project is PostgreSQL-only. Set DATABASE_URL to a PostgreSQL connection string.')

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    db.create_all()

    try:
        if not SystemConfig.query.first():
            db.session.add(SystemConfig(system_name='VISCANE', maintenance_mode=False))
            db.session.commit()
    except Exception:
        db.session.rollback()

def farmer_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('auth', mode='login'))
        user = User.query.get(session.get('user_id'))
        if not user or user.is_archived or not user.is_active:
            session.pop('user_id', None)
            return redirect(url_for('auth', mode='login'))
        return view(*args, **kwargs)
    return wrapped

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        admin_id = session.get('admin_id')
        if not admin_id:
            return redirect(url_for('admin_login'))
        admin = Admin.query.get(admin_id)
        if not admin or admin.is_archived:
            session.pop('admin_id', None)
            return redirect(url_for('admin_login'))
        return view(*args, **kwargs)
    return wrapped

def role_required(required_role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            admin_id = session.get('admin_id')
            if not admin_id:
                return redirect(url_for('admin_login'))
            admin = Admin.query.get(admin_id)
            if not admin or admin.is_archived or admin.role != required_role:
                return redirect(url_for('admin_portal'))
            return view(*args, **kwargs)
        return wrapped
    return decorator

def get_current_admin():
    admin_id = session.get('admin_id')
    if not admin_id:
        return None
    admin = Admin.query.get(admin_id)
    if not admin or admin.is_archived:
        return None
    return admin


def is_valid_admin_role(role):
    return role in {'admin', 'superadmin'}

def log_audit(action, user_id=None):
    try:
        entry = AuditLog(user_id=user_id, action=action)
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()

def get_system_config():
    config = SystemConfig.query.first()
    if not config:
        config = SystemConfig(system_name='VISCANE', maintenance_mode=False)
        db.session.add(config)
        db.session.commit()
    return config

def estimate_scan_metrics(scan):
    maturity = scan.maturity_pct or 0
    estimated_tch = round(40 + (maturity * 0.6), 2)
    estimated_lkg_tc = round(1.5 + (maturity * 0.01), 2)
    estimated_trash_pct = round(max(2, 12 - (maturity * 0.08)), 2)
    return estimated_tch, estimated_lkg_tc, estimated_trash_pct

def normalize_variety_name(variety):
    cleaned = (variety or "").strip()
    return VARIETY_ALIASES.get(cleaned, cleaned)

def normalize_cv_variety_name(variety):
    cleaned = (variety or "").strip()
    if not cleaned:
        return None
    cleaned_upper = cleaned.upper()
    mapped = CV_VARIETY_ALIASES.get(cleaned) or CV_VARIETY_ALIASES.get(cleaned_upper)
    if mapped:
        return mapped
    return normalize_variety_name(cleaned)

def normalize_cv_maturity_status(status):
    cleaned = (status or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not cleaned:
        return None
    if "OVER" in cleaned and "MATURE" in cleaned:
        return "OVER_MATURE"
    if "NOT" in cleaned and "MATURE" in cleaned:
        return "NOT_MATURE"
    if "MATURE" in cleaned:
        return "MATURE"
    return None

def get_cv_maturity_baseline_adjustment(variety, cv_maturity_status):
    normalized_variety = normalize_variety_name(variety)
    normalized_status = normalize_cv_maturity_status(cv_maturity_status)
    if not normalized_status:
        return 0.0, None
    weights = CV_MATURITY_BASELINE_WEIGHTS.get(normalized_variety) or {}
    return float(weights.get(normalized_status, 0.0)), normalized_status

def get_variety_weights(variety, custom_weights=None):
    weights = deepcopy(DEFAULT_VARIETY_WEIGHTS)
    if custom_weights is not None:
        weights[variety] = custom_weights
    return weights.get(variety, weights["VMC 84-524"])

def compute_visual_grade(visual_features):
    if not visual_features:
        raise ValueError("visual_features must not be empty.")
    return sum(visual_features) / len(visual_features)

def _parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def _parse_choice_value(value):
    if value is None:
        return None
    numeric_value = _parse_float(value)
    if numeric_value is not None:
        return numeric_value
    cleaned = str(value).strip()
    stage_map = {
        '1-Time': 1.0,
        '2-Time': 2.0,
        '3-Time': 3.0,
        '1x': 1.0,
        '2x': 2.0,
        '3x': 3.0,
    }
    return stage_map.get(cleaned)

def _parse_ratoon_value(value):
    if value is None:
        return None
    numeric_value = _parse_float(value)
    if numeric_value is not None:
        return numeric_value
    cleaned = str(value).strip()
    stage_map = {
        'Plant': 1.0,
        'Plant" (1st)': 1.0,
        '1st Ratoon': 2.0,
        '2nd Ratoon': 3.0,
        'New Plant (1)': 1.0,
        '1st ratoon (2nd)': 2.0,
        '2nd ratoon (3rd)': 3.0,
    }
    return stage_map.get(cleaned)

def _parse_hectares_value(value):
    if value is None:
        return None
    numeric_value = _parse_float(value)
    if numeric_value is not None:
        return numeric_value
    normalized = " ".join(
        str(value).strip().lower().replace('hectares', 'hectare').split()
    )
    hectares_map = {
        'less than 1 hectare': 0.5,
        '1 hectare': 1.0,
        '1-2': 1.5,
        '1-2 hectare': 1.5,
        '2': 2.0,
        '2 hectare': 2.0,
        '2-3': 2.5,
        '2-3 hectare': 2.5,
        '3': 3.0,
        '3 hectare': 3.0,
        '3-4': 3.5,
        '3-4 hectare': 3.5,
        '4': 4.0,
        '4 hectare': 4.0,
        '5': 5.0,
        '5 hectare': 5.0,
        'more than 5': 5.5,
        'more than 5 hectare': 5.5,
    }
    return hectares_map.get(normalized)

def _dataset_row_to_training_sample(row):
    variety = normalize_variety_name(row.get("variety"))
    hectares = _parse_hectares_value(row.get("hectares"))
    plowing = _parse_choice_value(row.get("plowing_count") or row.get("plowing"))
    weeding = _parse_choice_value(row.get("weeding_count") or row.get("weeding"))
    fertilizer = _parse_choice_value(row.get("fertilizer_count") or row.get("fertilizer"))
    ratoon = _parse_ratoon_value(row.get("ratoon_stage") or row.get("ratoon"))
    rssi = _parse_choice_value(row.get("rssi_infected") or row.get("rssi"))

    if not variety:
        return None
    numeric_values = {
        "hectares": hectares,
        "plowing": plowing,
        "weeding": weeding,
        "fertilizer": fertilizer,
        "ratoon": ratoon,
        "rssi": rssi,
    }
    if any(value is None for value in numeric_values.values()):
        return None

    targets = {
        "predicted_lkg_tc": _parse_float(row.get("predicted_lkg_tc")),
        "predicted_tc_ha": _parse_float(row.get("predicted_tc_ha")),
        "predicted_lkg": _parse_float(row.get("predicted_lkg")),
    }
    if all(value is None for value in targets.values()):
        return None

    return {
        "variety": variety,
        "hectares": hectares,
        "rssi": rssi,
        "weeding": weeding,
        "fertilizer": fertilizer,
        "ratoon": ratoon,
        "plowing": plowing,
        **targets,
    }

def _get_dataset_path():
    return os.getenv("AGRONOMIC_DATASET_PATH") or os.path.join(app.root_path, DEFAULT_DATASET_PATH)

def _build_training_pipeline():
    return Pipeline(
        steps=[
            ("vectorizer", DictVectorizer(sparse=False)),
            ("regressor", LinearRegression()),
        ]
    )

def _extract_features_and_target(samples, target):
    x_rows = [{key: row[key] for key in TRAINING_FEATURE_COLUMNS} for row in samples]
    y_rows = [float(row[target]) for row in samples]
    return x_rows, y_rows

def _compute_target_metrics(target_samples, target):
    x_rows, y_rows = _extract_features_and_target(target_samples, target)
    if len(target_samples) >= 16:
        x_train, x_test, y_train, y_test = train_test_split(
            x_rows,
            y_rows,
            test_size=0.25,
            random_state=42,
        )
        model = _build_training_pipeline()
        model.fit(x_train, y_train)
        y_pred = model.predict(x_test)
        r2_value = float(r2_score(y_test, y_pred))
        mae_value = float(mean_absolute_error(y_test, y_pred))
        evaluation_mode = "holdout"
    else:
        model = _build_training_pipeline()
        model.fit(x_rows, y_rows)
        y_pred = model.predict(x_rows)
        r2_value = float(r2_score(y_rows, y_pred))
        mae_value = float(mean_absolute_error(y_rows, y_pred))
        evaluation_mode = "train_only"

    accuracy_pct = max(0.0, min(100.0, r2_value * 100.0))
    return {
        "target": target,
        "label": TRAINING_TARGET_LABELS.get(target, target),
        "rows": len(target_samples),
        "mode": evaluation_mode,
        "r2": r2_value,
        "mae": mae_value,
        "accuracy_pct": accuracy_pct,
    }

def _compute_weighted_baseline_outputs(variety, hectares, agronomic_input):
    crop_stage = int(round(float(agronomic_input["ratoon"])))
    weights_used = get_variety_weights(variety)
    baseline_lkg_tc, baseline_tc_ha_per_hectare = get_sra_baseline(variety, crop_stage)
    adjusted_baseline_tc_ha = baseline_tc_ha_per_hectare * float(hectares)
    agronomic_adjustment = compute_agronomic_adjustment(agronomic_input, weights_used)
    agronomic_penalty = compute_agronomic_penalty(agronomic_input, weights_used)
    agronomic_multiplier = compute_agronomic_multiplier(agronomic_penalty)
    raw_predicted_lkg_tc = baseline_lkg_tc * agronomic_multiplier
    predicted_lkg_tc = max(0.0, min(baseline_lkg_tc, raw_predicted_lkg_tc))
    raw_predicted_tc_ha = adjusted_baseline_tc_ha * agronomic_multiplier
    predicted_tc_ha = max(0.0, min(adjusted_baseline_tc_ha, raw_predicted_tc_ha))
    predicted_lkg = predicted_lkg_tc * predicted_tc_ha
    return {
        "predicted_lkg_tc": predicted_lkg_tc,
        "predicted_tc_ha": predicted_tc_ha,
        "predicted_lkg": predicted_lkg,
        "agronomic_adjustment": agronomic_adjustment,
    }

def _build_legacy_teacher_samples():
    samples = []
    for variety in DEFAULT_VARIETY_WEIGHTS.keys():
        for hectares in LEGACY_TRAIN_HECTARES:
            for ratoon in LEGACY_TRAIN_LEVELS:
                for rssi in LEGACY_TRAIN_LEVELS:
                    for weeding in LEGACY_TRAIN_LEVELS:
                        for fertilizer in LEGACY_TRAIN_LEVELS:
                            for plowing in LEGACY_TRAIN_LEVELS:
                                agronomic_input = {
                                    "rssi": rssi,
                                    "weeding": weeding,
                                    "fertilizer": fertilizer,
                                    "ratoon": ratoon,
                                    "plowing": plowing,
                                }
                                outputs = _compute_weighted_baseline_outputs(
                                    variety=variety,
                                    hectares=hectares,
                                    agronomic_input=agronomic_input,
                                )
                                samples.append(
                                    {
                                        "variety": variety,
                                        "hectares": float(hectares),
                                        "rssi": float(rssi),
                                        "weeding": float(weeding),
                                        "fertilizer": float(fertilizer),
                                        "ratoon": float(ratoon),
                                        "plowing": float(plowing),
                                        "predicted_lkg_tc": outputs["predicted_lkg_tc"],
                                        "predicted_tc_ha": outputs["predicted_tc_ha"],
                                        "predicted_lkg": outputs["predicted_lkg"],
                                    }
                                )
    return samples

def _get_training_samples():
    if AGRONOMIC_TRAINING_MODE == "legacy_weighted":
        return _build_legacy_teacher_samples(), "legacy_weighted_teacher"
    samples = _load_csv_training_samples() + _load_db_training_samples()
    return samples, "dataset"

def generate_training_report():
    csv_samples = _load_csv_training_samples()
    db_samples = _load_db_training_samples()
    all_samples, sample_source = _get_training_samples()
    signature = _build_dataset_signature()
    training_bundle = _train_dataset_models(signature)
    trained_models = training_bundle.get("models", {})

    target_reports = []
    for target in TRAINING_TARGET_COLUMNS:
        target_samples = [sample for sample in all_samples if sample.get(target) is not None]
        if len(target_samples) < MIN_DATASET_ROWS:
            target_reports.append(
                {
                    "target": target,
                    "label": TRAINING_TARGET_LABELS.get(target, target),
                    "rows": len(target_samples),
                    "mode": "insufficient",
                    "r2": None,
                    "mae": None,
                    "accuracy_pct": None,
                }
            )
            continue
        target_reports.append(_compute_target_metrics(target_samples, target))

    return {
        "dataset_path": _get_dataset_path() if sample_source == "dataset" else "legacy_weighted_teacher (generated)",
        "csv_rows_used": len(csv_samples) if sample_source == "dataset" else 0,
        "db_rows_used": len(db_samples) if sample_source == "dataset" else 0,
        "total_rows_used": len(all_samples),
        "min_rows_required": MIN_DATASET_ROWS,
        "training_mode": AGRONOMIC_TRAINING_MODE,
        "engine_ready": bool(trained_models),
        "targets": target_reports,
    }

def export_training_report_files(training_report):
    reports_dir = os.path.join(app.root_path, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    generated_at_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    csv_latest = os.path.join(reports_dir, "training_results_latest.csv")
    json_latest = os.path.join(reports_dir, "training_results_latest.json")
    md_latest = os.path.join(reports_dir, "training_results_latest.md")

    csv_headers = [
        "generated_at_utc",
        "training_mode",
        "dataset_path",
        "total_rows_used",
        "target",
        "label",
        "rows",
        "mode",
        "r2",
        "mae",
        "accuracy_pct",
    ]
    with open(csv_latest, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_headers)
        writer.writeheader()
        for item in training_report.get("targets", []):
            writer.writerow(
                {
                    "generated_at_utc": generated_at_utc,
                    "training_mode": training_report.get("training_mode"),
                    "dataset_path": training_report.get("dataset_path"),
                    "total_rows_used": training_report.get("total_rows_used"),
                    "target": item.get("target"),
                    "label": item.get("label"),
                    "rows": item.get("rows"),
                    "mode": item.get("mode"),
                    "r2": item.get("r2"),
                    "mae": item.get("mae"),
                    "accuracy_pct": item.get("accuracy_pct"),
                }
            )

    with open(json_latest, "w", encoding="utf-8") as json_file:
        payload = dict(training_report)
        payload["generated_at_utc"] = generated_at_utc
        json.dump(payload, json_file, indent=2)

    table_lines = [
        "# Training Results (Latest)",
        "",
        f"- Generated (UTC): {generated_at_utc}",
        f"- Training mode: {training_report.get('training_mode')}",
        f"- Dataset path: {training_report.get('dataset_path')}",
        f"- Rows used: {training_report.get('total_rows_used')}",
        "",
        "| Target | Rows | Evaluation | R2 | MAE | Accuracy (%) |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for item in training_report.get("targets", []):
        r2_text = f"{item['r2']:.4f}" if item.get("r2") is not None else "N/A"
        mae_text = f"{item['mae']:.4f}" if item.get("mae") is not None else "N/A"
        acc_text = f"{item['accuracy_pct']:.2f}" if item.get("accuracy_pct") is not None else "N/A"
        table_lines.append(
            f"| {item.get('label')} | {item.get('rows', 0)} | {item.get('mode')} | {r2_text} | {mae_text} | {acc_text} |"
        )
    with open(md_latest, "w", encoding="utf-8") as md_file:
        md_file.write("\n".join(table_lines) + "\n")

    return {
        "generated_at_utc": generated_at_utc,
        "csv_path": csv_latest,
        "json_path": json_latest,
        "md_path": md_latest,
    }

def validate_training_csv_file(path):
    with open(path, newline="", encoding="utf-8") as dataset_file:
        reader = csv.DictReader(dataset_file)
        fieldnames = set(reader.fieldnames or [])
        missing_columns = sorted(TRAINING_REQUIRED_COLUMNS - fieldnames)
        if missing_columns:
            return False, f"CSV missing required columns: {', '.join(missing_columns)}."
        rows = list(reader)
    if not rows:
        return False, "CSV is empty."
    parsed_rows = [row for row in rows if _dataset_row_to_training_sample(row)]
    if len(parsed_rows) < MIN_DATASET_ROWS:
        return False, (
            f"CSV has only {len(parsed_rows)} valid training rows. "
            f"Minimum required is {MIN_DATASET_ROWS}."
        )
    return True, None

def _load_csv_training_samples():
    dataset_path = _get_dataset_path()
    if not os.path.isfile(dataset_path):
        return []

    samples = []
    with open(dataset_path, newline="", encoding="utf-8") as dataset_file:
        for row in csv.DictReader(dataset_file):
            sample = _dataset_row_to_training_sample(row)
            if sample:
                samples.append(sample)
    return samples

def _load_db_training_samples():
    samples = []
    logs = AgronomicLog.query.order_by(AgronomicLog.created_at.desc()).limit(500).all()
    for log in logs:
        sample = _dataset_row_to_training_sample(
            {
                "variety": log.variety,
                "hectares": log.hectares,
                "plowing_count": log.plowing_count,
                "weeding_count": log.weeding_count,
                "fertilizer_count": log.fertilizer_count,
                "ratoon_stage": log.ratoon_stage,
                "rssi_infected": log.rssi_infected,
                "predicted_lkg_tc": log.predicted_lkg_tc,
                "predicted_tc_ha": log.predicted_tc_ha,
                "predicted_lkg": log.predicted_lkg,
            }
        )
        if sample:
            samples.append(sample)
    return samples

def _build_dataset_signature():
    csv_signature = "none"
    dataset_path = _get_dataset_path()
    if os.path.isfile(dataset_path):
        csv_signature = f"{dataset_path}:{int(os.path.getmtime(dataset_path))}:{os.path.getsize(dataset_path)}"

    db_count = db.session.query(AgronomicLog.id).count()
    latest_log = db.session.query(AgronomicLog.created_at).order_by(AgronomicLog.created_at.desc()).first()
    latest_ts = latest_log[0].isoformat() if latest_log and latest_log[0] else "none"
    return csv_signature, db_count, latest_ts

@lru_cache(maxsize=8)
def _train_dataset_models(signature):
    del signature  # lru_cache key only; real data is reloaded below.
    samples, sample_source = _get_training_samples()
    if len(samples) < MIN_DATASET_ROWS:
        return {"models": {}, "row_count": len(samples), "sample_source": sample_source}

    trained_models = {}
    for target in TRAINING_TARGET_COLUMNS:
        target_samples = [sample for sample in samples if sample.get(target) is not None]
        if len(target_samples) < MIN_DATASET_ROWS:
            continue
        x_train, y_train = _extract_features_and_target(target_samples, target)
        model = _build_training_pipeline()
        model.fit(x_train, y_train)
        trained_models[target] = model

    return {"models": trained_models, "row_count": len(samples), "sample_source": sample_source}

def predict_from_dataset_models(variety, hectares, agronomic_input):
    signature = _build_dataset_signature()
    training_bundle = _train_dataset_models(signature)
    models = training_bundle.get("models", {})
    row_count = training_bundle.get("row_count", 0)
    sample_source = training_bundle.get("sample_source", "dataset")
    if not models:
        return None, row_count, sample_source

    feature_row = {
        "variety": variety,
        "hectares": float(hectares),
        "rssi": float(agronomic_input["rssi"]),
        "weeding": float(agronomic_input["weeding"]),
        "fertilizer": float(agronomic_input["fertilizer"]),
        "ratoon": float(agronomic_input["ratoon"]),
        "plowing": float(agronomic_input["plowing"]),
    }

    predictions = {}
    for target, model in models.items():
        predictions[target] = max(0.0, float(model.predict([feature_row])[0]))

    if "predicted_lkg" not in predictions and {
        "predicted_lkg_tc",
        "predicted_tc_ha",
    }.issubset(predictions.keys()):
        predictions["predicted_lkg"] = predictions["predicted_lkg_tc"] * predictions["predicted_tc_ha"]

    return predictions, row_count, sample_source

@lru_cache(maxsize=32)
def get_agronomic_linear_model(weight_signature):
    # Fit a tiny deterministic linear regressor so agronomic adjustment uses sklearn.
    x_train = []
    y_train = []
    for index, coefficient in enumerate(weight_signature):
        row = [0.0] * len(AGRONOMIC_KEYS)
        row[index] = 1.0
        x_train.append(row)
        y_train.append(float(coefficient))

    model = LinearRegression(fit_intercept=False)
    model.fit(x_train, y_train)
    return model

def compute_agronomic_adjustment(agronomic_input, weights):
    weight_signature = tuple(float(weights[key]) for key in AGRONOMIC_KEYS)
    model = get_agronomic_linear_model(weight_signature)
    features = [[float(agronomic_input[key]) for key in AGRONOMIC_KEYS]]
    return float(model.predict(features)[0])

def compute_agronomic_penalty(agronomic_input, weights):
    contributions = [float(agronomic_input[key]) * weights[key] for key in AGRONOMIC_KEYS]
    # Equation form: P = sum_k min(0, w_k * x_k)
    return sum(min(0.0, value) for value in contributions)

def _extract_cv_context(prediction_payload):
    if not isinstance(prediction_payload, dict):
        return {}

    models = prediction_payload.get("models")
    if not isinstance(models, dict) or not models:
        return {}

    best_entry = None
    best_confidence = float("-inf")
    for model_name, model_payload in models.items():
        if not isinstance(model_payload, dict):
            continue
        prediction = model_payload.get("prediction")
        if not isinstance(prediction, dict):
            continue
        confidence_raw = prediction.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = float("-inf")
        if confidence > best_confidence:
            best_confidence = confidence
            best_entry = {
                "model_name": model_name,
                "prediction": prediction,
                "top_k": model_payload.get("top_k") or [],
            }

    if not best_entry:
        return {}

    prediction = best_entry["prediction"]
    maturity_status = normalize_cv_maturity_status(prediction.get("maturity_status"))
    normalized_variety = normalize_cv_variety_name(prediction.get("variety"))

    visual_features = []
    top_k = best_entry.get("top_k") or []
    for item in top_k:
        if not isinstance(item, dict):
            continue
        try:
            conf_value = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        visual_features.append(conf_value)

    if not visual_features:
        try:
            primary_conf = float(prediction.get("confidence"))
            visual_features = [primary_conf]
        except (TypeError, ValueError):
            visual_features = []

    return {
        "model_name": best_entry["model_name"],
        "maturity_status": maturity_status,
        "class_name": prediction.get("class_name"),
        "variety": prediction.get("variety"),
        "normalized_variety": normalized_variety,
        "confidence": prediction.get("confidence"),
        "visual_features": visual_features,
        "models": models,
    }


def _build_cv_upload_path(filename):
    _, ext = os.path.splitext(filename or "")
    ext = ext.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    generated_name = f"cv-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(8)}{ext}"
    relative_path = os.path.join(CV_UPLOAD_RELATIVE_DIR, generated_name)
    absolute_path = os.path.join(app.static_folder, relative_path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    return relative_path.replace("\\", "/"), absolute_path


def _persist_cv_upload(user_id, uploaded_filename, file_bytes, cv_context):
    if not user_id or not file_bytes:
        return

    relative_path, absolute_path = _build_cv_upload_path(uploaded_filename)
    try:
        with open(absolute_path, "wb") as image_file:
            image_file.write(file_bytes)
    except OSError:
        return

    try:
        confidence = _parse_float((cv_context or {}).get("confidence"))
        entry = CvScanUpload(
            user_id=user_id,
            image_path=relative_path,
            original_filename=secure_filename(uploaded_filename) or "upload.jpg",
            variety=((cv_context or {}).get("normalized_variety") or (cv_context or {}).get("variety")),
            maturity_status=(cv_context or {}).get("maturity_status"),
            model_name=(cv_context or {}).get("model_name"),
            confidence=confidence,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()

def compute_agronomic_multiplier(agronomic_adjustment):
    return max(0.0, 1.0 + agronomic_adjustment)

def get_sra_baseline(variety, crop_stage):
    try:
        return SRA_BASELINE_LKG_TC[variety][crop_stage], SRA_BASELINE_TC_HA[variety][crop_stage]
    except KeyError as exc:
        raise ValueError("Missing SRA baseline for the selected variety or ratoon stage.") from exc

def predict_variety_metrics(variety, hectares, visual_features, agronomic_input, custom_weights=None, cv_maturity_status=None):
    normalized_variety = normalize_variety_name(variety)
    if normalized_variety not in DEFAULT_VARIETY_WEIGHTS and custom_weights is None:
        raise ValueError("Unknown variety. Provide a known variety or include custom_weights.")

    crop_stage = int(round(float(agronomic_input["ratoon"])))
    if crop_stage not in CROP_STAGE_LABELS:
        raise ValueError("ratoon stage must be 1, 2, or 3.")

    weights_used = get_variety_weights(normalized_variety, custom_weights)
    visual_grade = compute_visual_grade(visual_features)
    baseline_lkg_tc, baseline_tc_ha_per_hectare = get_sra_baseline(normalized_variety, crop_stage)
    adjusted_baseline_tc_ha = baseline_tc_ha_per_hectare * hectares
    # Keep sklearn in use via compute_agronomic_adjustment(), but enforce weighted-baseline prediction output.
    agronomic_adjustment = compute_agronomic_adjustment(agronomic_input, weights_used)
    agronomic_penalty = compute_agronomic_penalty(agronomic_input, weights_used)
    cv_maturity_adjustment, normalized_cv_maturity_status = get_cv_maturity_baseline_adjustment(
        normalized_variety, cv_maturity_status
    )
    combined_penalty = agronomic_penalty + cv_maturity_adjustment
    agronomic_multiplier = compute_agronomic_multiplier(combined_penalty)
    raw_predicted_lkg_tc = baseline_lkg_tc * agronomic_multiplier
    predicted_lkg_tc = max(0.0, min(baseline_lkg_tc, raw_predicted_lkg_tc))
    raw_predicted_tc_ha = adjusted_baseline_tc_ha * agronomic_multiplier
    predicted_tc_ha = max(0.0, min(adjusted_baseline_tc_ha, raw_predicted_tc_ha))
    predicted_lkg = predicted_lkg_tc * predicted_tc_ha
    prediction_engine = "weighted_baseline_sklearn"
    trained_rows = 0

    predicted_quality_grade = visual_grade + agronomic_adjustment + cv_maturity_adjustment

    return {
        "variety": normalized_variety,
        "crop_stage": CROP_STAGE_LABELS[crop_stage],
        "hectares": hectares,
        "visual_grade": visual_grade,
        "agronomic_adjustment": agronomic_adjustment,
        "agronomic_penalty": agronomic_penalty,
        "cv_maturity_status": normalized_cv_maturity_status,
        "cv_maturity_adjustment": cv_maturity_adjustment,
        "combined_penalty": combined_penalty,
        "agronomic_multiplier": agronomic_multiplier,
        "predicted_quality_grade": predicted_quality_grade,
        "baseline_lkg_tc": baseline_lkg_tc,
        "baseline_tc_ha_per_hectare": baseline_tc_ha_per_hectare,
        "adjusted_baseline_tc_ha": adjusted_baseline_tc_ha,
        "raw_predicted_lkg_tc": raw_predicted_lkg_tc,
        "predicted_lkg_tc": predicted_lkg_tc,
        "raw_predicted_tc_ha": raw_predicted_tc_ha,
        "predicted_tc_ha": predicted_tc_ha,
        "predicted_lkg": predicted_lkg,
        "weights_used": weights_used,
        "prediction_engine": prediction_engine,
        "training_rows_used": trained_rows,
        "input": {
            "hectares": hectares,
            "visual_features": visual_features,
            "agronomic_input": agronomic_input,
            "cv_maturity_status": normalized_cv_maturity_status,
        },
    }

def _format_factor_value(value):
    if value is None:
        return "missing"
    if float(value).is_integer():
        return str(int(float(value)))
    return f"{float(value):.2f}"

def generate_recommendations(prediction_response, agronomic_input, missing_fields=None, variety=None):
    recommendations = []
    missing_fields = missing_fields or []
    agronomic_input = agronomic_input or {}
    selected_variety = normalize_variety_name(
        variety
        or prediction_response.get("variety")
        or "VMC 84-524"
    )
    fertilizer_guide = FERTILIZER_TIMING_GUIDE.get(selected_variety, FERTILIZER_TIMING_GUIDE["VMC 84-524"])
    weeding_guide = WEEDING_GUIDE.get(selected_variety, WEEDING_GUIDE["VMC 84-524"])
    plowing_guide = PLOWING_GUIDE.get(selected_variety, PLOWING_GUIDE["VMC 84-524"])
    normalized_cv_maturity = normalize_cv_maturity_status(
        prediction_response.get("cv_maturity_status")
        or prediction_response.get("maturity_status")
    )

    if normalized_cv_maturity == "NOT_MATURE":
        recommendations.append(
            {
                "icon": "pause-circle-outline",
                "title": "Delay cutting for sucrose accumulation",
                "meta": (
                    'Real-time harvest directive: "Not Mature" classification indicates stalks should not be cut yet. '
                    "Delay harvest to allow optimal sucrose accumulation before scheduling transport."
                ),
                "tag": "Advisory",
                "tag_class": "warning",
                "category": "Harvest Directives",
            }
        )
    elif normalized_cv_maturity == "MATURE":
        recommendations.append(
            {
                "icon": "checkmark-done-circle-outline",
                "title": "Finalize immediate harvest logistics",
                "meta": (
                    'Real-time harvest directive: "Mature" classification indicates harvest-ready stalks. '
                    "Proceed with immediate cutting and finalize hauling/transport coordination."
                ),
                "tag": "Ready",
                "tag_class": "success",
                "category": "Harvest Directives",
            }
        )
    elif normalized_cv_maturity == "OVER_MATURE":
        recommendations.append(
            {
                "icon": "alert-outline",
                "title": "Expedite harvest to prevent further yield loss",
                "meta": (
                    'Real-time harvest directive: "Over Mature" classification requires urgent cutting. '
                    "Expedite harvest to mitigate further yield degradation caused by sucrose inversion."
                ),
                "tag": "Urgent",
                "tag_class": "warning",
                "category": "Harvest Directives",
            }
        )

    if missing_fields:
        recommendations.append(
            {
                "icon": "alert-circle-outline",
                "title": "Complete missing agronomic inputs",
                "meta": "Please fill: " + ", ".join(missing_fields) + ".",
                "tag": "Required",
                "tag_class": "warning",
                "category": "Missing Inputs",
            }
        )

    fertilizer_value = agronomic_input.get("fertilizer")
    fertilizer_missing = ("fertilizer" in missing_fields) or (fertilizer_value is None)
    if fertilizer_missing:
        recommendations.append(
            {
                "icon": "flask-outline",
                "title": f"Choose fertilizer timing for {selected_variety}",
                "meta": (
                    f"{fertilizer_guide[1]} "
                    f"{fertilizer_guide[2]} "
                    f"{fertilizer_guide[3]}"
                ),
                "tag": "Required",
                "tag_class": "warning",
                "category": "Fertilizer Guidance",
            }
        )

    weeding_value = agronomic_input.get("weeding")
    weeding_missing = ("weeding" in missing_fields) or (weeding_value is None)
    if weeding_missing:
        recommendations.append(
            {
                "icon": "cut-outline",
                "title": f"Choose weeding schedule for {selected_variety}",
                "meta": (
                    f"{weeding_guide[1]} "
                    f"{weeding_guide[2]} "
                    f"{weeding_guide[3]}"
                ),
                "tag": "Required",
                "tag_class": "warning",
                "category": "Weeding Guidance",
            }
        )

    plowing_value = agronomic_input.get("plowing")
    plowing_missing = ("plowing" in missing_fields) or (plowing_value is None)
    if plowing_missing:
        recommendations.append(
            {
                "icon": "construct-outline",
                "title": f"Choose plowing schedule for {selected_variety}",
                "meta": (
                    f"{plowing_guide[1]} "
                    f"{plowing_guide[2]} "
                    f"{plowing_guide[3]}"
                ),
                "tag": "Required",
                "tag_class": "warning",
                "category": "Plowing Guidance",
            }
        )

    predicted_lkg = prediction_response.get("predicted_lkg")
    baseline_lkg_tc = prediction_response.get("baseline_lkg_tc")
    adjusted_baseline_tc_ha = prediction_response.get("adjusted_baseline_tc_ha")
    baseline_lkg = None
    if baseline_lkg_tc is not None and adjusted_baseline_tc_ha is not None:
        baseline_lkg = float(baseline_lkg_tc) * float(adjusted_baseline_tc_ha)

    low_lkg = False
    if predicted_lkg is not None and baseline_lkg and baseline_lkg > 0:
        low_lkg = (float(predicted_lkg) / baseline_lkg) < 0.85

    low_factor_rules = [
        ("plowing", 2.0, "Increase plowing"),
        ("weeding", 2.0, "Increase weeding"),
        ("fertilizer", 2.0, "Increase fertilizer"),
    ]

    for key, threshold, label in low_factor_rules:
        value = agronomic_input.get(key)
        if value is None:
            continue
        if float(value) < threshold:
            if key == "fertilizer":
                category = "Fertilizer Guidance"
            elif key == "weeding":
                category = "Weeding Guidance"
            elif key == "plowing":
                category = "Plowing Guidance"
            else:
                category = "Yield Improvement"
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"{label} from {_format_factor_value(value)} to at least {int(threshold)}",
                    "meta": "Low input level is pulling down predicted LKG.",
                    "tag": "Improve",
                    "tag_class": "warning",
                    "category": category,
                }
            )

    rssi_value = agronomic_input.get("rssi")
    if rssi_value is not None and float(rssi_value) >= 1.0:
        recommendations.append(
            {
                "icon": "medkit-outline",
                "title": "RSSI infected: apply PHILSURIN control protocol",
                "meta": (
                    "Conduct weekly monitoring, especially lower leaf areas, and immediately remove and burn "
                    "infested leaves to prevent spread. Effective chemical options include Carbofuran, "
                    "Phenthoate, Dinotefuran, Thiamethoxam, Pymetrozine, and Buprofezin. Report suspected "
                    "infestations to PHILSURIN, DA, or SRA, and adopt integrated pest management using "
                    "monitoring, physical removal, and chemical or biological interventions."
                ),
                "tag": "Urgent",
                "tag_class": "warning",
                "category": "Pest and Disease",
            }
        )

    if not fertilizer_missing and low_lkg:
        fert_choice = int(round(float(fertilizer_value)))
        fert_choice = max(1, min(3, fert_choice))
        recommendations.append(
            {
                "icon": "leaf-outline",
                "title": f"Follow {fert_choice}-time fertilizer schedule for {selected_variety}",
                "meta": fertilizer_guide[fert_choice],
                "tag": "Guide",
                "tag_class": "",
                "category": "Fertilizer Guidance",
            }
        )

    if not fertilizer_missing:
        fert_choice = int(round(float(fertilizer_value)))
        fert_choice = max(1, min(3, fert_choice))
        if fert_choice == 1:
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 2-time fertilizer application for {selected_variety}",
                    "meta": fertilizer_guide[2],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Fertilizer Guidance",
                }
            )
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 3-time fertilizer application for {selected_variety}",
                    "meta": fertilizer_guide[3],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Fertilizer Guidance",
                }
            )
        elif fert_choice == 2:
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 3-time fertilizer application for {selected_variety}",
                    "meta": fertilizer_guide[3],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Fertilizer Guidance",
                }
            )

    if not weeding_missing and low_lkg:
        weed_choice = int(round(float(weeding_value)))
        weed_choice = max(1, min(3, weed_choice))
        recommendations.append(
            {
                "icon": "leaf-outline",
                "title": f"Follow {weed_choice}-time weeding schedule for {selected_variety}",
                "meta": weeding_guide[weed_choice],
                "tag": "Guide",
                "tag_class": "",
                "category": "Weeding Guidance",
            }
        )

    if not weeding_missing:
        weed_choice = int(round(float(weeding_value)))
        weed_choice = max(1, min(3, weed_choice))
        if weed_choice == 1:
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 2-time weeding for {selected_variety}",
                    "meta": weeding_guide[2],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Weeding Guidance",
                }
            )
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 3-time weeding for {selected_variety}",
                    "meta": weeding_guide[3],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Weeding Guidance",
                }
            )
        elif weed_choice == 2:
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 3-time weeding for {selected_variety}",
                    "meta": weeding_guide[3],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Weeding Guidance",
                }
            )

    if not plowing_missing and low_lkg:
        plow_choice = int(round(float(plowing_value)))
        plow_choice = max(1, min(3, plow_choice))
        recommendations.append(
            {
                "icon": "leaf-outline",
                "title": f"Follow {plow_choice}-time plowing schedule for {selected_variety}",
                "meta": plowing_guide[plow_choice],
                "tag": "Guide",
                "tag_class": "",
                "category": "Plowing Guidance",
            }
        )

    if not plowing_missing:
        plow_choice = int(round(float(plowing_value)))
        plow_choice = max(1, min(3, plow_choice))
        ratoon_value = agronomic_input.get("ratoon")
        ratoon_stage = int(round(float(ratoon_value))) if ratoon_value is not None else None
        is_vmc_947_ratoon = selected_variety == "VMC 84-947" and ratoon_stage in {2, 3}

        if is_vmc_947_ratoon:
            recommendations.append(
                {
                    "icon": "construct-outline",
                    "title": "For VMC 84-947 ratoon, avoid plowing",
                    "meta": "Use stubble shaving and inter-row cultivation (off-barring) to protect ratoon shoots.",
                    "tag": "Important",
                    "tag_class": "warning",
                    "category": "Plowing Guidance",
                }
            )
        elif plow_choice == 1:
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 2-time plowing for {selected_variety}",
                    "meta": plowing_guide[2],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Plowing Guidance",
                }
            )
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 3-time plowing for {selected_variety}",
                    "meta": plowing_guide[3],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Plowing Guidance",
                }
            )
        elif plow_choice == 2:
            recommendations.append(
                {
                    "icon": "trending-up-outline",
                    "title": f"Consider 3-time plowing for {selected_variety}",
                    "meta": plowing_guide[3],
                    "tag": "Upgrade",
                    "tag_class": "warning",
                    "category": "Plowing Guidance",
                }
            )

    if low_lkg and not recommendations:
        recommendations.append(
            {
                "icon": "analytics-outline",
                "title": "Predicted LKG is below baseline",
                "meta": "Increase low agronomic inputs and re-calculate to recover yield.",
                "tag": "Attention",
                "tag_class": "warning",
                "category": "Yield Improvement",
            }
        )

    if not low_lkg and not recommendations:
        recommendations.append(
            {
                "icon": "checkmark-circle-outline",
                "title": "Maintain current agronomic practices",
                "meta": "Current inputs are supporting baseline-level yield.",
                "tag": "Stable",
                "tag_class": "success",
                "category": "General",
            }
        )

    return recommendations

def group_recommendations_by_category(recommendations):
    grouped = {}
    for recommendation in (recommendations or []):
        category = recommendation.get("category") or "General"
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(recommendation)

    category_order = [
        "Missing Inputs",
        "Harvest Directives",
        "Pest and Disease",
        "Fertilizer Guidance",
        "Weeding Guidance",
        "Plowing Guidance",
        "Yield Improvement",
        "General",
    ]

    ordered_groups = []
    for category in category_order:
        if category in grouped:
            ordered_groups.append({"category": category, "items": grouped.pop(category)})

    for category, items in grouped.items():
        ordered_groups.append({"category": category, "items": items})

    return ordered_groups


def _build_multipart_form_data(fields, files):
    """Encode multipart/form-data payload for outbound prediction requests."""
    boundary = f"----ViscaneBoundary{secrets.token_hex(16)}"
    chunks = []

    for field_name, field_value in (fields or {}).items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(field_value).encode("utf-8"))
        chunks.append(b"\r\n")

    for file_item in (files or []):
        field_name = file_item["field_name"]
        file_name = file_item["filename"]
        content_type = file_item["content_type"]
        file_bytes = file_item["content"]
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{file_name}"\r\n'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(file_bytes)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type

def verify_and_upgrade_password(user, raw_password):
    try:
        if check_password_hash(user.password, raw_password):
            return True
    except Exception:
        pass
    if user.password == raw_password:
        user.password = generate_password_hash(raw_password)
        db.session.commit()
        return True
    return False

@app.route('/')
def portal():
    # The Welcome Page
    return render_template('portal.html')

@app.route('/homepage')
@farmer_login_required
def homepage():
    # Farmer Dashboard
    user = User.query.get(session.get('user_id'))
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('auth', mode='login'))

    today = datetime.utcnow().date()
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    sample_plot_names = ('Plot #1 Sample', 'Plot #2 Sample', 'Plot #4 Sample')
    scans_base_query = Scan.query.filter(
        Scan.user_id == user.id,
        ~Scan.plot_name.in_(sample_plot_names)
    )
    scans_today = scans_base_query.filter(
        Scan.created_at >= datetime(today.year, today.month, today.day)
    ).count()
    pending_scans = scans_base_query.filter(Scan.status == 'pending').count()
    scans_last7 = scans_base_query.filter(Scan.created_at >= seven_days_ago).all()
    recent_scans = scans_base_query.order_by(Scan.created_at.desc()).limit(3).all()
    recent_cv_uploads = CvScanUpload.query.filter_by(user_id=user.id).order_by(CvScanUpload.created_at.desc()).limit(12).all()
    recent_scan_cards = []
    for index, scan in enumerate(recent_scans):
        recent_scan_cards.append({
            "scan": scan,
            "cv_upload": recent_cv_uploads[index] if index < len(recent_cv_uploads) else None,
        })
    if not recent_scan_cards and recent_cv_uploads:
        for upload in recent_cv_uploads[:3]:
            recent_scan_cards.append({
                "scan": None,
                "cv_upload": upload,
            })
    agronomic_logs = AgronomicLog.query.filter_by(user_id=user.id).order_by(AgronomicLog.created_at.desc()).limit(10).all()
    announcements = Notification.query.order_by(Notification.created_at.desc()).limit(5).all()
    recommendations = session.get('farmer_recommendations') or DEFAULT_RECOMMENDATIONS
    grouped_recommendations = group_recommendations_by_category(recommendations)

    if scans_last7:
        grade_a = sum(1 for s in scans_last7 if s.grade.upper() == 'A')
        avg_grade_a = int((grade_a / len(scans_last7)) * 100)
        avg_maturity = int(sum(s.maturity_pct for s in scans_last7) / len(scans_last7))
    else:
        avg_grade_a = 0
        avg_maturity = 0

    if avg_maturity >= 85:
        yield_est = "High"
        harvest_window = "3-7 days"
    elif avg_maturity >= 75:
        yield_est = "Medium"
        harvest_window = "8-12 days"
    else:
        yield_est = "Low"
        harvest_window = "14-18 days"

    message = request.args.get('message')
    error = request.args.get('error')

    return render_template(
        'homepage.html',
        user=user,
        scans_today=scans_today,
        pending_scans=pending_scans,
        avg_grade_a=avg_grade_a,
        yield_est=yield_est,
        harvest_window=harvest_window,
        avg_maturity=avg_maturity,
        recent_scans=recent_scans,
        recent_scan_cards=recent_scan_cards,
        recent_cv_uploads=recent_cv_uploads,
        agronomic_logs=agronomic_logs,
        announcements=announcements,
        recommendations=recommendations,
        grouped_recommendations=grouped_recommendations,
        message=message,
        error=error
    )

@app.route('/farmer/recommendations')
@farmer_login_required
def farmer_recommendations():
    user = User.query.get(session.get('user_id'))
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('auth', mode='login'))

    recommendations = session.get('farmer_recommendations') or DEFAULT_RECOMMENDATIONS
    grouped_recommendations = group_recommendations_by_category(recommendations)
    return render_template(
        'farmer_recommendations.html',
        user=user,
        recommendations=recommendations,
        grouped_recommendations=grouped_recommendations,
    )

@app.route('/farmer/agronomic-logs')
@farmer_login_required
def farmer_agronomic_logs():
    user = User.query.get(session.get('user_id'))
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('auth', mode='login'))

    agronomic_logs = AgronomicLog.query.filter_by(user_id=user.id).order_by(AgronomicLog.created_at.desc()).all()
    return render_template(
        'farmer_agronomic_logs.html',
        user=user,
        agronomic_logs=agronomic_logs,
    )


@app.route('/farmer/cv-upload/<int:upload_id>/delete', methods=['POST'])
@farmer_login_required
def delete_cv_upload(upload_id):
    user_id = session.get('user_id')
    upload = CvScanUpload.query.filter_by(id=upload_id, user_id=user_id).first()
    if not upload:
        return redirect(url_for('homepage', error='Picture not found or already removed.'))

    file_path = os.path.join(app.static_folder, upload.image_path or "")
    try:
        db.session.delete(upload)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return redirect(url_for('homepage', error='Unable to remove picture right now.'))

    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
    except OSError:
        pass

    return redirect(url_for('homepage', message='Picture removed from recent scans.'))


@app.route('/api/scan/predict', methods=['POST'])
@farmer_login_required
def api_scan_predict():
    uploaded_file = request.files.get('file') or request.files.get('image')
    if not uploaded_file or not uploaded_file.filename:
        return {"error": "Missing image file. Use form field `file`."}, 400

    try:
        top_k_raw = request.args.get(
            'top_k',
            str(os.getenv('SCAN_PREDICT_TOP_K', DEFAULT_SCAN_PREDICT_TOP_K))
        )
        top_k = max(1, min(10, int(top_k_raw)))
    except (TypeError, ValueError):
        return {"error": "Invalid `top_k`. Provide an integer from 1 to 10."}, 400

    endpoint = (
        os.getenv('SCAN_PREDICT_ENDPOINT', DEFAULT_SCAN_PREDICT_ENDPOINT).strip()
        or DEFAULT_SCAN_PREDICT_ENDPOINT
    )
    timeout_raw = os.getenv('SCAN_PREDICT_TIMEOUT_SECONDS', str(DEFAULT_SCAN_PREDICT_TIMEOUT_SECONDS))
    try:
        timeout_seconds = max(5.0, float(timeout_raw))
    except (TypeError, ValueError):
        timeout_seconds = float(DEFAULT_SCAN_PREDICT_TIMEOUT_SECONDS)

    file_bytes = uploaded_file.read()
    if not file_bytes:
        return {"error": "Uploaded image is empty."}, 400

    separator = '&' if '?' in endpoint else '?'
    target_url = f"{endpoint}{separator}{urlencode({'top_k': top_k})}"
    body, content_type = _build_multipart_form_data(
        fields={},
        files=[
            {
                "field_name": "file",
                "filename": secure_filename(uploaded_file.filename) or "capture.jpg",
                "content_type": uploaded_file.mimetype or "image/jpeg",
                "content": file_bytes,
            }
        ],
    )

    outbound = Request(target_url, data=body, method='POST')
    outbound.add_header('accept', 'application/json')
    outbound.add_header('Content-Type', content_type)
    outbound.add_header('Content-Length', str(len(body)))

    try:
        with urlopen(outbound, timeout=timeout_seconds) as api_response:
            response_body = api_response.read()
            status_code = getattr(api_response, 'status', 200)
    except HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode('utf-8', errors='replace')
        except Exception:
            details = str(exc)
        return {
            "error": "Prediction service returned an error.",
            "status": exc.code,
            "details": details[:600],
        }, 502
    except URLError as exc:
        return {
            "error": "Prediction service is unreachable.",
            "details": str(exc.reason) if getattr(exc, "reason", None) else str(exc),
        }, 502
    except TimeoutError:
        return {
            "error": "Prediction service timed out.",
            "details": f"Request exceeded {timeout_seconds:.0f} seconds.",
        }, 504
    except Exception as exc:
        return {
            "error": "Failed to request prediction service.",
            "details": str(exc),
        }, 500

    if 200 <= status_code < 300:
        try:
            cv_context = {}
            decoded_payload = json.loads(response_body.decode("utf-8"))
            cv_context = _extract_cv_context(decoded_payload)
            if cv_context:
                session["latest_cv_context"] = cv_context
            _persist_cv_upload(
                user_id=session.get("user_id"),
                uploaded_filename=uploaded_file.filename,
                file_bytes=file_bytes,
                cv_context=cv_context,
            )
        except Exception:
            pass

    return Response(response_body, status=status_code, mimetype='application/json')

@app.route('/calculate', methods=['POST'])
@farmer_login_required
def calculate_results():
    user = User.query.get(session.get('user_id'))
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('auth', mode='login'))

    variety = request.form.get('variety', '').strip()
    plowing_count = request.form.get('plowing_count', '').strip()
    weeding_count = request.form.get('weeding_count', '').strip()
    fertilizer_count = request.form.get('fertilizer_count', '').strip()
    ratoon_stage = request.form.get('ratoon_stage', '').strip()
    rssi_infected = request.form.get('rssi_infected', '').strip()
    hectares = request.form.get('hectares', '').strip()
    cv_maturity_status = request.form.get('cv_maturity_status', '').strip()
    cv_variety_detected = request.form.get('cv_variety_detected', '').strip()
    cv_prediction_applied = request.form.get('cv_prediction_applied', '').strip() in {'1', 'true', 'True'}
    cv_context = (session.get("latest_cv_context") or {}) if cv_prediction_applied else {}
    cv_detected_variety = normalize_cv_variety_name(cv_variety_detected or cv_context.get("normalized_variety") or cv_context.get("variety"))
    if not cv_maturity_status:
        cv_maturity_status = (cv_context.get("maturity_status") or "").strip()

    latest_scan = Scan.query.filter_by(user_id=user.id).order_by(Scan.created_at.desc()).first()
    maturity_pct = latest_scan.maturity_pct if latest_scan else None

    visual_features = [0.21, 0.48, 0.63, 0.74, 0.59]
    cv_visual_features = cv_context.get("visual_features")
    if isinstance(cv_visual_features, list):
        cleaned_features = []
        for value in cv_visual_features:
            try:
                cleaned_features.append(float(value))
            except (TypeError, ValueError):
                continue
        if cleaned_features:
            visual_features = cleaned_features
    rssi_text = (rssi_infected or '').strip().lower()
    if rssi_text in {'yes', 'y', '1', 'true'}:
        rssi_value = 1.0
    elif rssi_text in {'no', 'n', '0', 'false'}:
        rssi_value = 0.0
    else:
        rssi_value = None

    def parse_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def parse_numeric_choice(value):
        if value is None:
            return None
        cleaned = str(value).strip()
        parsed = parse_float(cleaned)
        if parsed is not None:
            return parsed
        for ch in cleaned:
            if ch.isdigit():
                return float(ch)
        return None

    def parse_ratoon_stage(value):
        if value is None:
            return None

        cleaned = " ".join(str(value).strip().lower().split())
        stage_map = {
            '1': 1.0,
            'plant': 1.0,
            'new plant': 1.0,
            'new plant (1)': 1.0,
            '2': 2.0,
            '1st ratoon': 2.0,
            '1st ratoon (2nd)': 2.0,
            '3': 3.0,
            '2nd ratoon': 3.0,
            '2nd ratoon (3rd)': 3.0,
        }
        if cleaned in stage_map:
            return stage_map[cleaned]

        return parse_float(cleaned)

    def parse_hectares(value):
        if value is None:
            return None

        normalized = " ".join(
            value.strip().lower().replace('hectares', 'hectare').split()
        )

        hectares_map = {
            'less than 1 hectare': 0.5,
            '1 hectare': 1.0,
            '1-2': 1.5,
            '1-2 hectare': 1.5,
            '2': 2.0,
            '2 hectare': 2.0,
            '2-3': 2.5,
            '2-3 hectare': 2.5,
            '3': 3.0,
            '3 hectare': 3.0,
            '3-4': 3.5,
            '3-4 hectare': 3.5,
            '4': 4.0,
            '4 hectare': 4.0,
            '5': 5.0,
            '5 hectare': 5.0,
            'more than 5': 5.5,
            'more than 5 hectare': 5.5,
        }
        if normalized in hectares_map:
            return hectares_map[normalized]

        try:
            return float(normalized)
        except (TypeError, ValueError):
            return None

    def crop_stage_label(value):
        crop_stage_map = {
            '1': 'New Plant (1)',
            '2': '1st ratoon (2nd)',
            '3': '2nd ratoon (3rd)',
            'plant': 'New Plant (1)',
            'Plant': 'New Plant (1)',
            'new plant (1)': 'New Plant (1)',
            '1st ratoon (2nd)': '1st ratoon (2nd)',
            '2nd ratoon (3rd)': '2nd ratoon (3rd)',
            'Plant" (1st)': 'Plant cane (1st)',
            '1st Ratoon': '1st ratoon (2nd)',
            '2nd Ratoon': '2nd ratoon (3rd)'
        }
        return crop_stage_map.get((value or '').strip(), 'Unknown')

    api_error = None
    prediction_response = {}
    missing_fields = []
    hectares_value = parse_hectares(hectares)
    normalized_variety = normalize_variety_name(variety)
    plowing_value = parse_numeric_choice(plowing_count)
    weeding_value = parse_numeric_choice(weeding_count)
    fertilizer_value = parse_numeric_choice(fertilizer_count)
    ratoon_value = parse_ratoon_stage(ratoon_stage)

    payload = {
        "variety": normalized_variety,
        "hectares": hectares_value,
        "visual_features": visual_features,
        "agronomic_input": {
            "rssi": rssi_value,
            "weeding": weeding_value,
            "fertilizer": fertilizer_value,
            "ratoon": ratoon_value,
            "plowing": plowing_value
        },
        "custom_weights": {
            "rssi": -0.50,
            "weeding": 0.32,
            "fertilizer": 0.22,
            "ratoon": -0.10,
            "plowing": 0.09
        },
        "cv_maturity_status": cv_maturity_status,
    }

    has_complete_payload = (
        bool(variety)
        and hectares_value is not None
        and len(visual_features) >= 1
        and all(isinstance(value, (int, float)) for value in visual_features)
        and all(value is not None for value in payload["agronomic_input"].values())
    )

    if has_complete_payload and cv_detected_variety and normalized_variety and cv_detected_variety != normalized_variety:
        has_complete_payload = False
        api_error = (
            f"Variety mismatch: Computer vision detected {cv_detected_variety}, "
            f"but agronomic input selected {normalized_variety}. Please select the matching variety."
        )
        missing_fields.append("matching variety")

    if has_complete_payload:
        try:
            prediction_response = predict_variety_metrics(
                variety=payload["variety"],
                hectares=payload["hectares"],
                visual_features=payload["visual_features"],
                agronomic_input=payload["agronomic_input"],
                custom_weights=payload["custom_weights"],
                cv_maturity_status=payload["cv_maturity_status"],
            )
        except ValueError as exc:
            api_error = str(exc)
        except Exception as exc:
            api_error = f"Prediction request failed: {exc}"
    else:
        if not variety:
            missing_fields.append("variety")
        if hectares_value is None:
            missing_fields.append("hectares")
        if rssi_value is None:
            missing_fields.append("rssi")
        if weeding_value is None:
            missing_fields.append("weeding")
        if fertilizer_value is None:
            missing_fields.append("fertilizer")
        if ratoon_value is None:
            missing_fields.append("ratoon stage")
        if plowing_value is None:
            missing_fields.append("plowing")
        if missing_fields:
            api_error = "Missing required fields for prediction: " + ", ".join(missing_fields) + "."
        else:
            api_error = "Missing required fields for prediction."

    recommendation_input = {
        "rssi": rssi_value,
        "weeding": weeding_value,
        "fertilizer": fertilizer_value,
        "ratoon": ratoon_value,
        "plowing": plowing_value,
    }
    generated_recommendations = generate_recommendations(
        prediction_response=prediction_response,
        agronomic_input=recommendation_input,
        missing_fields=missing_fields,
        variety=normalized_variety,
    )
    session['farmer_recommendations'] = generated_recommendations

    recommendations_summary = " | ".join(
        f"{item.get('category', 'General')}: {item.get('title', '')}"
        for item in generated_recommendations
    )
    try:
        agronomic_log = AgronomicLog(
            user_id=user.id,
            variety=normalized_variety or variety or None,
            hectares=hectares or None,
            plowing_count=plowing_count or None,
            weeding_count=weeding_count or None,
            fertilizer_count=fertilizer_count or None,
            ratoon_stage=ratoon_stage or None,
            rssi_infected=rssi_infected or None,
            predicted_lkg_tc=prediction_response.get('predicted_lkg_tc'),
            predicted_tc_ha=prediction_response.get('predicted_tc_ha'),
            predicted_lkg=prediction_response.get('predicted_lkg'),
            recommendations_summary=recommendations_summary or "No recommendation generated.",
        )
        db.session.add(agronomic_log)
        db.session.commit()
    except Exception:
        db.session.rollback()

    def maturity_label(value):
        if value is None:
            return 'Not provided'
        if value < 75:
            return 'Not Mature'
        if value <= 90:
            return 'Mature'
        return 'Over Mature'

    variety_display = normalized_variety or 'Not provided'
    normalized_cv_maturity = normalize_cv_maturity_status(
        prediction_response.get("cv_maturity_status") or cv_maturity_status
    )
    if normalized_cv_maturity == "NOT_MATURE":
        maturity_display = "Not Mature (Computer Vision)"
    elif normalized_cv_maturity == "OVER_MATURE":
        maturity_display = "Over Mature (Computer Vision)"
    elif normalized_cv_maturity == "MATURE":
        maturity_display = "Mature (Computer Vision)"
    else:
        maturity_display = maturity_label(maturity_pct)
    hectares_display = hectares if hectares else 'Not provided'
    lkg_tc_display = 'Pending'
    predicted_lkg_tc_display = 'Pending'
    predicted_tc_ha_display = 'Pending'

    def format_decimal(value):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return 'Pending'

    crop_stage_display = prediction_response.get('crop_stage') or crop_stage_label(ratoon_stage)
    visual_grade_display = format_decimal(prediction_response.get('visual_grade'))
    agronomic_adjustment_display = format_decimal(prediction_response.get('agronomic_adjustment'))
    agronomic_multiplier_display = format_decimal(prediction_response.get('agronomic_multiplier'))
    predicted_quality_grade_display = format_decimal(prediction_response.get('predicted_quality_grade'))
    baseline_lkg_tc_display = format_decimal(prediction_response.get('baseline_lkg_tc'))
    baseline_tc_ha_per_hectare_display = format_decimal(prediction_response.get('baseline_tc_ha_per_hectare'))
    adjusted_baseline_tc_ha_display = format_decimal(prediction_response.get('adjusted_baseline_tc_ha'))
    predicted_lkg_tc_display = format_decimal(prediction_response.get('predicted_lkg_tc'))
    predicted_tc_ha_display = format_decimal(prediction_response.get('predicted_tc_ha'))
    predicted_lkg_display = format_decimal(prediction_response.get('predicted_lkg'))
    weights_used = prediction_response.get('weights_used') or {}
    prediction_input = prediction_response.get('input') or {}
    api_visual_features = prediction_input.get('visual_features') or visual_features
    api_agronomic_input = prediction_input.get('agronomic_input') or payload["agronomic_input"]
    api_hectares = prediction_input.get('hectares', hectares_value)
    try:
        cv_confidence_pct = f"{float(cv_context.get('confidence')) * 100:.1f}%"
    except (TypeError, ValueError, AttributeError):
        cv_confidence_pct = "Pending"

    return render_template(
        'calculate_results.html',
        user=user,
        variety_display=variety_display,
        maturity_display=maturity_display,
        lkg_tc_display=lkg_tc_display,
        hectares_display=hectares_display,
        predicted_lkg_tc_display=predicted_lkg_tc_display,
        predicted_tc_ha_display=predicted_tc_ha_display,
        crop_stage_display=crop_stage_display,
        visual_grade_display=visual_grade_display,
        agronomic_adjustment_display=agronomic_adjustment_display,
        agronomic_multiplier_display=agronomic_multiplier_display,
        predicted_quality_grade_display=predicted_quality_grade_display,
        baseline_lkg_tc_display=baseline_lkg_tc_display,
        baseline_tc_ha_per_hectare_display=baseline_tc_ha_per_hectare_display,
        adjusted_baseline_tc_ha_display=adjusted_baseline_tc_ha_display,
        predicted_lkg_display=predicted_lkg_display,
        cv_model_display=(cv_context.get("model_name") if cv_context else None),
        cv_class_display=(cv_context.get("class_name") if cv_context else None),
        cv_variety_display=(cv_detected_variety if cv_detected_variety else None),
        cv_maturity_display=(normalized_cv_maturity if normalized_cv_maturity else None),
        cv_confidence_display=cv_confidence_pct,
        weights_used=weights_used,
        api_visual_features=api_visual_features,
        api_agronomic_input=api_agronomic_input,
        api_hectares=api_hectares,
        api_error=api_error,
        plowing_count=plowing_count,
        weeding_count=weeding_count,
        fertilizer_count=fertilizer_count,
        ratoon_stage=ratoon_stage,
        rssi_infected=rssi_infected
    )

@app.route('/farmer/settings', methods=['GET', 'POST'])
@farmer_login_required
def farmer_settings():
    user = User.query.get(session.get('user_id'))
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('auth', mode='login'))

    error = None
    success = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_profile':
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            province = request.form.get('province', '').strip()
            municipality = request.form.get('municipality', '').strip()
            barangay = request.form.get('barangay', '').strip()

            if not email or not phone or not province or not municipality or not barangay:
                error = 'Please complete your profile details.'
            elif len(phone) != 11 or not phone.isdigit():
                error = 'Phone number must be exactly 11 digits.'
            else:
                existing_user = User.query.filter(User.email == email, User.id != user.id).first()
                if existing_user:
                    error = 'Email already exists.'
                else:
                    user.email = email
                    user.phone = phone
                    user.province = province
                    user.municipality = municipality
                    user.barangay = barangay
                    db.session.commit()
                    log_audit(f"Farmer updated profile details: {user.fullname}", user_id=user.id)
                    success = 'Profile updated successfully.'
        else:
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not current_password or not new_password or not confirm_password:
                error = 'Please complete all password fields.'
            elif not verify_and_upgrade_password(user, current_password):
                error = 'Current password is incorrect.'
            elif new_password != confirm_password:
                error = 'New passwords do not match.'
            elif current_password == new_password:
                error = 'New password must be different from the current password.'
            else:
                user.password = generate_password_hash(new_password)
                db.session.commit()
                log_audit(f"Farmer updated password: {user.fullname}", user_id=user.id)
                success = 'Password updated successfully.'

    return render_template('farmer_settings.html', user=user, error=error, success=success)

@app.route('/farmer/feedback', methods=['POST'])
@farmer_login_required
def farmer_feedback():
    user = User.query.get(session.get('user_id'))
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('auth', mode='login'))

    feedback_message = request.form.get('feedback_message', '').strip()
    if not feedback_message:
        return redirect(url_for('homepage', error='Please enter your feedback before submitting.'))

    feedback_entry = Feedback(user_id=user.id, message=feedback_message)
    db.session.add(feedback_entry)
    db.session.commit()
    log_audit(f"Farmer feedback submitted by {user.fullname}", user_id=user.id)
    return redirect(url_for('homepage', message='Thank you. Your feedback was submitted successfully.'))

@app.route('/admin')
@login_required
def admin_portal():
    # Admin Dashboard
    current_admin = get_current_admin()
    total_users = User.query.filter_by(is_archived=False, is_active=True).count()
    total_scans = Scan.query.count()
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    active_user_ids = db.session.query(User.id).filter(
        User.is_archived.is_(False),
        User.is_active.is_(True)
    ).subquery()
    active_farmers = db.session.query(Scan.user_id).filter(
        Scan.created_at >= seven_days_ago,
        Scan.user_id.in_(active_user_ids)
    ).distinct().count()
    pending_scans = Scan.query.filter(
        Scan.status == 'pending',
        Scan.user_id.in_(active_user_ids)
    ).count()
    users = User.query.filter_by(is_archived=False, is_active=True).order_by(User.id.desc()).limit(6).all()
    now = datetime.utcnow()
    logs = [
        {
            "icon": "server-outline",
            "title": "Database Backup",
            "meta": "Nightly recovery snapshot completed successfully.",
            "status": "Success",
            "color": "#2E7D32",
            "timestamp": now - timedelta(hours=1, minutes=12),
        },
        {
            "icon": "warning-outline",
            "title": "Failed Login Attempt",
            "meta": "IP: 192.168.1.45 exceeded retry threshold.",
            "status": "Alert",
            "color": "#C62828",
            "timestamp": now - timedelta(hours=2, minutes=4),
        },
        {
            "icon": "person-add-outline",
            "title": "New User Registration",
            "meta": "Maria Santos is awaiting farmer account review.",
            "status": "Review",
            "color": "#1565C0",
            "timestamp": now - timedelta(hours=4, minutes=18),
        },
    ]
    model_accuracy = 98.6
    storage_utilization = 68
    try:
        usage = disk_usage(os.getcwd())
        if usage.total > 0:
            storage_utilization = round((usage.used / usage.total) * 100, 1)
    except Exception:
        pass
    stats = {
        "active_users": total_users,
        "total_scans": total_scans,
        "active_farmers": active_farmers,
        "pending_scans": pending_scans,
        "storage_utilization": storage_utilization,
    }

    metric_trends = {
        "active_users": "+12% from last week" if total_users else "Waiting for first users",
        "total_scans": "+18% from last week" if total_scans else "Waiting for first scan",
        "active_farmers": "Last 7 days",
        "pending_scans": "Needs review" if pending_scans else "All clear",
        "storage_utilization": "Steady vs last week" if storage_utilization < 70 else "+6% from last week",
    }

    for log in logs:
        timestamp = log.get("timestamp")
        if not timestamp:
            continue
        elapsed = now - timestamp
        total_minutes = max(1, int(elapsed.total_seconds() // 60))
        if total_minutes < 60:
            relative = f"{total_minutes}m ago"
        else:
            total_hours = total_minutes // 60
            if total_hours < 24:
                relative = f"{total_hours}h ago"
            else:
                relative = f"{total_hours // 24}d ago"
        log["relative_time"] = relative
        log["exact_time"] = timestamp.strftime("%b %d, %Y %I:%M %p UTC")
    return render_template(
        'admin.html',
        total_users=total_users,
        users=users,
        logs=logs,
        current_admin=current_admin,
        stats=stats,
        metric_trends=metric_trends,
    )

@app.route('/admin/farmers', methods=['GET', 'POST'])
@login_required
def admin_farmers():
    message = request.args.get('message')
    error = request.args.get('error')
    search = request.args.get('search', '').strip()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            fullname = request.form.get('fullname', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            province = request.form.get('province', '').strip()
            municipality = request.form.get('municipality', '').strip()
            barangay = request.form.get('barangay', '').strip()
            password = request.form.get('password', '').strip()
            if not fullname or not email or not phone or not password:
                return redirect(url_for('admin_farmers', error='Please complete all required fields.'))
            if User.query.filter_by(email=email).first():
                return redirect(url_for('admin_farmers', error='Email already exists.'))
            new_user = User(
                fullname=fullname,
                email=email,
                phone=phone,
                password=generate_password_hash(password),
                province=province,
                municipality=municipality,
                barangay=barangay,
                is_active=True,
                is_archived=False
            )
            db.session.add(new_user)
            db.session.commit()
            log_audit(f"Admin created farmer account: {fullname}", user_id=get_current_admin().id if get_current_admin() else None)
            return redirect(url_for('admin_farmers', message='Farmer account created successfully.'))

        if action == 'reset':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user and not user.is_archived:
                temp_password = "12345"
                user.password = generate_password_hash(temp_password)
                db.session.commit()
                log_audit(f"Farmer credentials reset: {user.fullname}", user_id=get_current_admin().id if get_current_admin() else None)
                return redirect(url_for('admin_farmers', message=f"Temporary password for {user.fullname}: {temp_password}"))
            return redirect(url_for('admin_farmers', error='Unable to reset credentials.'))

        if action == 'deactivate':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user and not user.is_archived and user.is_active:
                user.is_active = False
                db.session.commit()
                log_audit(f"Farmer account deactivated: {user.fullname}", user_id=get_current_admin().id if get_current_admin() else None)
                return redirect(url_for('admin_farmers', message=f"{user.fullname} has been deactivated."))
            return redirect(url_for('admin_farmers', error='Unable to deactivate account.'))

        if action == 'activate':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user and not user.is_archived and not user.is_active:
                user.is_active = True
                db.session.commit()
                log_audit(f"Farmer account activated: {user.fullname}", user_id=get_current_admin().id if get_current_admin() else None)
                return redirect(url_for('admin_farmers', message=f"{user.fullname} has been reactivated."))
            return redirect(url_for('admin_farmers', error='Unable to activate account.'))

    users_query = User.query.filter_by(is_archived=False)
    if search:
        like_term = f"%{search}%"
        users_query = users_query.filter(
            (User.fullname.ilike(like_term)) |
            (User.email.ilike(like_term)) |
            (User.phone.ilike(like_term)) |
            (User.province.ilike(like_term)) |
            (User.municipality.ilike(like_term)) |
            (User.barangay.ilike(like_term))
        )
    users = users_query.order_by(User.id.desc()).all()
    active_users = [user for user in users if user.is_active]
    inactive_users = [user for user in users if not user.is_active]
    return render_template(
        'admin_farmers.html',
        users=active_users,
        inactive_users=inactive_users,
        message=message,
        error=error,
        search=search,
        current_admin=get_current_admin()
    )

@app.route('/admin/farmers/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_farmer_edit(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_archived:
        return redirect(url_for('admin_farmers'))

    if request.method == 'POST':
        fullname = request.form.get('fullname', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        province = request.form.get('province', '').strip()
        municipality = request.form.get('municipality', '').strip()
        barangay = request.form.get('barangay', '').strip()

        if not fullname or not email or not phone:
            return redirect(url_for('admin_farmer_edit', user_id=user.id, error='Please complete all required fields.'))

        existing_user = User.query.filter(User.email == email, User.id != user.id).first()
        if existing_user:
            return redirect(url_for('admin_farmer_edit', user_id=user.id, error='Email already exists.'))

        user.fullname = fullname
        user.email = email
        user.phone = phone
        user.province = province
        user.municipality = municipality
        user.barangay = barangay
        db.session.commit()
        log_audit(f"Farmer account updated: {user.fullname}", user_id=get_current_admin().id if get_current_admin() else None)
        return redirect(url_for('admin_farmers', message='Farmer account updated.'))

    error = request.args.get('error')
    return render_template('admin_farmer_edit.html', user=user, error=error, current_admin=get_current_admin())

@app.route('/admin/monitoring')
@login_required
def admin_monitoring():
    logs = AgronomicLog.query.order_by(AgronomicLog.created_at.desc()).limit(50).all()
    monitoring_rows = []
    for log in logs:
        monitoring_rows.append({
            "farmer_name": log.user.fullname if log.user else f"User #{log.user_id}",
            "variety": log.variety or "N/A",
            "hectares": log.hectares or "N/A",
            "predicted_lkg_tc": round(log.predicted_lkg_tc, 2) if log.predicted_lkg_tc is not None else None,
            "predicted_tc_ha": round(log.predicted_tc_ha, 2) if log.predicted_tc_ha is not None else None,
            "predicted_lkg": round(log.predicted_lkg, 2) if log.predicted_lkg is not None else None,
            "rssi_infected": log.rssi_infected or "N/A",
            "created_at": log.created_at
        })
    return render_template('admin_monitoring.html', rows=monitoring_rows, current_admin=get_current_admin())

@app.route('/admin/models', methods=['GET', 'POST'])
@login_required
def admin_models():
    current_admin = get_current_admin()
    if current_admin and current_admin.role == 'superadmin':
        return redirect(url_for('superadmin_settings'))
    return redirect(url_for('admin_portal'))

@app.route('/admin/reports')
@login_required
def admin_reports():
    logs = AgronomicLog.query.order_by(AgronomicLog.created_at.desc()).all()
    farmer_summary = {}
    for log in logs:
        entry = farmer_summary.setdefault(log.user_id, {
            "count": 0,
            "lkg_tc_count": 0,
            "tc_ha_count": 0,
            "total_lkg_tc": 0.0,
            "total_tc_ha": 0.0,
            "total_lkg": 0.0,
        })
        entry["count"] += 1
        if log.predicted_lkg_tc is not None:
            entry["lkg_tc_count"] += 1
            entry["total_lkg_tc"] += float(log.predicted_lkg_tc)
        if log.predicted_tc_ha is not None:
            entry["tc_ha_count"] += 1
            entry["total_tc_ha"] += float(log.predicted_tc_ha)
        if log.predicted_lkg is not None:
            entry["total_lkg"] += float(log.predicted_lkg)

    rows = []
    for user_id, summary in farmer_summary.items():
        user = User.query.get(user_id)
        if not user or user.is_archived:
            continue
        count = summary["count"]
        rows.append({
            "name": user.fullname,
            "municipality": user.municipality or 'N/A',
            "barangay": user.barangay or 'N/A',
            "predictions": count,
            "avg_lkg_tc": round(summary["total_lkg_tc"] / summary["lkg_tc_count"], 2) if summary["lkg_tc_count"] else 0,
            "avg_lkg_ha": round(summary["total_tc_ha"] / summary["tc_ha_count"], 2) if summary["tc_ha_count"] else 0,
            "total_lkg": round(summary["total_lkg"], 2),
        })

    rows = sorted(rows, key=lambda item: item["predictions"], reverse=True)
    return render_template('admin_reports.html', rows=rows, current_admin=get_current_admin())

@app.route('/admin/communications', methods=['GET', 'POST'])
@login_required
def admin_communications():
    message = None
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('message', '').strip()
        if title and content:
            notification = Notification(
                title=title,
                message=content,
                created_by=get_current_admin().id if get_current_admin() else None
            )
            db.session.add(notification)
            db.session.commit()
            log_audit(f"Announcement published: {title}", user_id=get_current_admin().id if get_current_admin() else None)
            message = 'Announcement published.'

    notifications = Notification.query.order_by(Notification.created_at.desc()).limit(10).all()
    feedback_entries = Feedback.query.order_by(Feedback.created_at.desc()).limit(20).all()
    feedback = []
    for entry in feedback_entries:
        farmer = User.query.get(entry.user_id) if entry.user_id else None
        feedback.append({
            "farmer_label": farmer.fullname if farmer else (f"Farmer ID {entry.user_id}" if entry.user_id else "Unknown"),
            "message": entry.message,
            "created_at": entry.created_at
        })
    return render_template(
        'admin_communications.html',
        notifications=notifications,
        feedback=feedback,
        message=message,
        current_admin=get_current_admin()
    )

@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    if not Admin.query.filter_by(is_archived=False).first():
        return redirect(url_for('admin_setup'))
    error = None
    success = request.args.get('success')
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip().lower()
        password = request.form.get('password', '')
        admin = Admin.query.filter(
            ((Admin.username.ilike(identifier)) | (Admin.email.ilike(identifier))) & (Admin.is_archived.is_(False))
        ).first()
        if admin and check_password_hash(admin.password_hash, password):
            session['admin_id'] = admin.id
            return redirect(url_for('admin_portal'))
        error = 'Invalid admin credentials. Please try again.'
    return render_template('admin_login.html', error=error, success=success)

@app.route('/superadmin-login', methods=['GET', 'POST'])
def superadmin_login():
    if not Admin.query.filter_by(is_archived=False).first():
        return redirect(url_for('admin_setup'))
    error = None
    success = request.args.get('success')
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip().lower()
        password = request.form.get('password', '')
        admin = Admin.query.filter(
            ((Admin.username.ilike(identifier)) | (Admin.email.ilike(identifier))) & (Admin.is_archived.is_(False))
        ).first()
        if admin and check_password_hash(admin.password_hash, password):
            if admin.role != 'superadmin':
                error = 'Your account is not authorized for superadmin access.'
            else:
                session['admin_id'] = admin.id
                return redirect(url_for('superadmin_portal'))
        else:
            error = 'Invalid superadmin credentials. Please try again.'
    return render_template('superadmin_login.html', error=error, success=success)

@app.route('/admin-setup', methods=['GET', 'POST'])
def admin_setup():
    if Admin.query.filter_by(is_archived=False).first():
        return redirect(url_for('admin_login'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not username or not email or not password:
            error = 'Please complete all fields.'
        elif password != confirm:
            error = 'Passwords do not match.'
        elif Admin.query.filter((Admin.username.ilike(username)) | (Admin.email.ilike(email))).first():
            error = 'An admin account with those details already exists.'
        else:
            admin = Admin(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role='superadmin'
            )
            db.session.add(admin)
            db.session.commit()
            session['admin_id'] = admin.id
            log_audit(f"Superadmin account created: {admin.username}", user_id=admin.id)
            return redirect(url_for('admin_portal'))
    return render_template('admin_setup.html', error=error)

@app.route('/admin-register', methods=['GET', 'POST'])
def admin_register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        role = request.form.get('role', 'admin').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username or not email or not password or not confirm_password:
            error = 'Please complete all registration fields.'
        elif not is_valid_admin_role(role):
            error = 'Invalid role selected.'
        elif password != confirm_password:
            error = 'Passwords do not match.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif Admin.query.filter(Admin.username.ilike(username)).first():
            error = 'Username is already taken.'
        elif Admin.query.filter(Admin.email.ilike(email)).first():
            error = 'Email is already registered.'
        else:
            admin = Admin(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
                is_archived=False,
            )
            db.session.add(admin)
            db.session.commit()
            log_audit(f"{role.title()} account registered: {username}")
            return redirect(url_for('admin_login', success=f'{role.title()} account created successfully.'))

    return render_template('admin_register.html', error=error)

@app.route('/superadmin-register', methods=['GET', 'POST'])
def superadmin_register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username or not email or not password or not confirm_password:
            error = 'Please complete all registration fields.'
        elif password != confirm_password:
            error = 'Passwords do not match.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif Admin.query.filter(Admin.username.ilike(username)).first():
            error = 'Username is already taken.'
        elif Admin.query.filter(Admin.email.ilike(email)).first():
            error = 'Email is already registered.'
        else:
            admin = Admin(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role='superadmin',
                is_archived=False,
            )
            db.session.add(admin)
            db.session.commit()
            log_audit(f"Superadmin account registered: {username}")
            return redirect(url_for('superadmin_login', success='Superadmin account created successfully.'))

    return render_template('superadmin_register.html', error=error)

@app.route('/admin-reset', methods=['GET', 'POST'])
def admin_reset():
    error = None
    success = None
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        new_password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if new_password != confirm:
            error = 'Passwords do not match.'
        else:
            admin = Admin.query.filter(
                (Admin.username.ilike(identifier)) | (Admin.email.ilike(identifier))
            ).first()
            if not admin or admin.email.lower() != email:
                error = 'Admin account not found with those details.'
            else:
                admin.password_hash = generate_password_hash(new_password)
                db.session.commit()
                success = 'Password updated. You can sign in now.'
    return render_template('admin_reset.html', error=error, success=success)

@app.route('/superadmin')
@role_required('superadmin')
def superadmin_portal():
    create_error = request.args.get('create_error')
    create_success = request.args.get('create_success')
    total_users = User.query.filter_by(is_archived=False, is_active=True).count()
    active_user_count = User.query.filter_by(is_archived=False, is_active=True).count()
    deactivated_user_count = User.query.filter_by(is_archived=False, is_active=False).count()
    archived_user_count = User.query.filter_by(is_archived=True).count()
    total_admins = Admin.query.filter_by(is_archived=False).count()
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    active_user_ids = db.session.query(User.id).filter(
        User.is_archived.is_(False),
        User.is_active.is_(True)
    ).subquery()
    active_farmers = db.session.query(Scan.user_id).filter(
        Scan.created_at >= seven_days_ago,
        Scan.user_id.in_(active_user_ids)
    ).distinct().count()
    total_scans = Scan.query.count()
    total_prediction_logs = AgronomicLog.query.count()
    total_estimated_lkg_value = db.session.query(db.func.coalesce(db.func.sum(AgronomicLog.predicted_lkg), 0.0)).scalar() or 0.0
    total_estimated_lkg = f"{float(total_estimated_lkg_value):,.2f}"
    pending_scans = Scan.query.filter(
        Scan.status == 'pending',
        Scan.user_id.in_(active_user_ids)
    ).count()
    admins = Admin.query.filter_by(is_archived=False).order_by(Admin.id.desc()).all()
    users = User.query.filter_by(is_archived=False, is_active=True).order_by(User.id.desc()).limit(8).all()
    archived_users = User.query.filter_by(is_archived=True).order_by(User.id.desc()).all()
    deactivated_users = User.query.filter_by(is_archived=False, is_active=False).order_by(User.id.desc()).all()
    recent_scans = Scan.query.filter(Scan.user_id.in_(active_user_ids)).order_by(Scan.created_at.desc()).limit(6).all()
    recent_predictions = AgronomicLog.query.order_by(AgronomicLog.created_at.desc()).limit(6).all()
    superadmin_cv_uploads = CvScanUpload.query.order_by(CvScanUpload.created_at.desc()).all()
    return render_template(
        'superadmin.html',
        total_users=total_users,
        active_user_count=active_user_count,
        deactivated_user_count=deactivated_user_count,
        archived_user_count=archived_user_count,
        total_admins=total_admins,
        active_farmers=active_farmers,
        total_scans=total_scans,
        total_prediction_logs=total_prediction_logs,
        total_estimated_lkg=total_estimated_lkg,
        pending_scans=pending_scans,
        admins=admins,
        users=users,
        archived_users=archived_users,
        deactivated_users=deactivated_users,
        recent_scans=recent_scans,
        recent_predictions=recent_predictions,
        superadmin_cv_uploads=superadmin_cv_uploads,
        create_error=create_error,
        create_success=create_success,
        current_admin=get_current_admin()
    )

@app.route('/superadmin/admins/create', methods=['POST'])
@role_required('superadmin')
def superadmin_create_admin():
    current_admin = get_current_admin()
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip().lower()
    role = request.form.get('role', 'admin').strip().lower()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not username or not email or not password or not confirm_password:
        return redirect(url_for('superadmin_portal', create_error='Please complete all registration fields.'))
    if not is_valid_admin_role(role):
        return redirect(url_for('superadmin_portal', create_error='Invalid role selected.'))
    if password != confirm_password:
        return redirect(url_for('superadmin_portal', create_error='Passwords do not match.'))
    if len(password) < 8:
        return redirect(url_for('superadmin_portal', create_error='Password must be at least 8 characters.'))

    existing_username = Admin.query.filter(Admin.username.ilike(username)).first()
    if existing_username:
        return redirect(url_for('superadmin_portal', create_error='Username is already taken.'))
    existing_email = Admin.query.filter(Admin.email.ilike(email)).first()
    if existing_email:
        return redirect(url_for('superadmin_portal', create_error='Email is already registered.'))

    new_admin = Admin(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        is_archived=False,
    )
    db.session.add(new_admin)
    db.session.commit()
    if current_admin:
        log_audit(
            f"{role.title()} account created: {username}",
            user_id=current_admin.id,
        )
    return redirect(url_for('superadmin_portal', create_success=f'{role.title()} account created successfully.'))

@app.route('/superadmin/admins/role', methods=['POST'])
@role_required('superadmin')
def superadmin_update_role():
    admin_id = request.form.get('admin_id')
    role = request.form.get('role', 'admin')
    current_admin = get_current_admin()
    admin = Admin.query.get(admin_id)
    if admin and not admin.is_archived and current_admin and admin.id != current_admin.id and is_valid_admin_role(role):
        admin.role = role
        db.session.commit()
        log_audit(f"Admin role updated for {admin.username} to {role}", user_id=current_admin.id)
    return redirect(url_for('superadmin_portal'))

@app.route('/superadmin/admins/archive', methods=['POST'])
@role_required('superadmin')
def superadmin_archive_admin():
    admin_id = request.form.get('admin_id')
    current_admin = get_current_admin()
    admin = Admin.query.get(admin_id)
    if admin and current_admin and admin.id != current_admin.id:
        admin.is_archived = True
        db.session.commit()
        log_audit(f"Admin account archived: {admin.username}", user_id=current_admin.id)
    return redirect(url_for('superadmin_portal'))

@app.route('/superadmin/users/archive', methods=['POST'])
@role_required('superadmin')
def superadmin_archive_user():
    user_id = request.form.get('user_id')
    current_admin = get_current_admin()
    user = User.query.get(user_id)
    if user and not user.is_archived:
        user.is_archived = True
        db.session.commit()
        log_audit(f"User account archived: {user.fullname}", user_id=current_admin.id if current_admin else None)
    return redirect(url_for('superadmin_portal'))

@app.route('/superadmin/users/<int:user_id>')
@role_required('superadmin')
def superadmin_user_details(user_id):
    user = User.query.get_or_404(user_id)
    scans = Scan.query.filter_by(user_id=user.id).order_by(Scan.created_at.desc()).all()
    agronomic_logs = AgronomicLog.query.filter_by(user_id=user.id).order_by(AgronomicLog.created_at.desc()).all()
    feedback_entries = Feedback.query.filter_by(user_id=user.id).order_by(Feedback.created_at.desc()).all()
    audit_logs = AuditLog.query.filter(
        AuditLog.action.ilike(f"%{user.fullname}%")
    ).order_by(AuditLog.timestamp.desc()).all()

    activity_items = []
    for scan in scans:
        activity_items.append({
            'kind': 'Scan',
            'title': scan.plot_name,
            'meta': f"Grade {scan.grade} | Maturity {scan.maturity_pct}% | Status {scan.status.title()}",
            'timestamp': scan.created_at,
        })
    for log in agronomic_logs:
        activity_items.append({
            'kind': 'Agronomic Log',
            'title': log.variety or 'Agronomic entry',
            'meta': f"Hectares {log.hectares or 'N/A'} | Predicted LKG {round(log.predicted_lkg, 2) if log.predicted_lkg is not None else 'N/A'}",
            'timestamp': log.created_at,
        })
    for entry in feedback_entries:
        preview = entry.message[:90] + ('...' if len(entry.message) > 90 else '')
        activity_items.append({
            'kind': 'Feedback',
            'title': 'Farmer feedback submitted',
            'meta': preview,
            'timestamp': entry.created_at,
        })
    for log in audit_logs:
        activity_items.append({
            'kind': 'Audit',
            'title': log.action,
            'meta': f"Actor ID: {log.user_id if log.user_id is not None else 'System'}",
            'timestamp': log.timestamp,
        })

    activity_items.sort(
        key=lambda item: item['timestamp'] or datetime.min,
        reverse=True,
    )

    return render_template(
        'superadmin_user_details.html',
        user=user,
        scans=scans,
        agronomic_logs=agronomic_logs,
        feedback_entries=feedback_entries,
        audit_logs=audit_logs,
        activity_items=activity_items[:25],
        current_admin=get_current_admin(),
    )

@app.route('/superadmin/users/restore', methods=['POST'])
@role_required('superadmin')
def superadmin_restore_user():
    user_id = request.form.get('user_id')
    current_admin = get_current_admin()
    user = User.query.get(user_id)
    if user and user.is_archived:
        user.is_archived = False
        db.session.commit()
        log_audit(f"User account restored: {user.fullname}", user_id=current_admin.id if current_admin else None)
    return redirect(url_for('superadmin_portal'))

@app.route('/admin-logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect(url_for('portal'))

@app.route('/auth', methods=['GET', 'POST'])
def auth():
    mode = request.args.get('mode', 'login')
    
    if request.method == 'POST':
        if mode == 'register':
            fullname = request.form.get('fullname', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            password = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')
            province = request.form.get('province', '').strip()
            municipality = request.form.get('municipality', '').strip()
            barangay = request.form.get('barangay', '').strip()
            if not fullname or not email or not phone or not password:
                return render_template('auth.html', mode=mode, error='Please complete all required fields.')
            if password != confirm:
                return render_template('auth.html', mode=mode, error='Passwords do not match.')
            existing = User.query.filter_by(email=email).first()
            if existing:
                return render_template('auth.html', mode=mode, error='Email already registered.')
            new_user = User(
                fullname=fullname,
                email=email,
                phone=phone,
                password=generate_password_hash(password),
                province=province,
                municipality=municipality,
                barangay=barangay
            )
            db.session.add(new_user)
            db.session.commit()
            session['user_id'] = new_user.id
            return redirect(url_for('auth_register_success'))

        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and not user.is_archived and user.is_active and verify_and_upgrade_password(user, password):
            session['user_id'] = user.id
            return redirect(url_for('homepage'))
        if user and user.is_archived:
            return render_template('auth.html', mode=mode, error='Account is archived. Please contact support.')
        if user and not user.is_active:
            return render_template('auth.html', mode=mode, error='Account is deactivated. Please contact support.')
        return render_template('auth.html', mode=mode, error='Invalid credentials. Please try again.')
    
    # Handle HTMX requests for switching forms
    if request.headers.get('HX-Request'):
        return render_template('auth_form.html', mode=mode)
    
    return render_template('auth.html', mode=mode)

@app.route('/auth/register-success')
@farmer_login_required
def auth_register_success():
    user = User.query.get(session.get('user_id'))
    if not user:
        return redirect(url_for('auth', mode='login'))
    return render_template('auth_register_success.html', user=user)

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('portal'))

@app.route('/scan/new', methods=['GET', 'POST'])
@farmer_login_required
def scan_new():
    error = None
    if request.method == 'POST':
        plot_name = request.form.get('plot_name', '').strip()
        grade = request.form.get('grade', '').strip().upper()
        maturity_pct = request.form.get('maturity_pct', '').strip()
        status = request.form.get('status', 'pending').strip().lower()
        if not plot_name or not grade or not maturity_pct:
            error = 'Please complete all fields.'
        else:
            try:
                maturity_value = int(maturity_pct)
            except ValueError:
                maturity_value = None
            if maturity_value is None or maturity_value < 0 or maturity_value > 100:
                error = 'Maturity must be between 0 and 100.'
            else:
                scan = Scan(
                    user_id=session.get('user_id'),
                    plot_name=plot_name,
                    grade=grade,
                    maturity_pct=maturity_value,
                    status=status
                )
                db.session.add(scan)
                db.session.commit()
                log_audit(f"User {session.get('user_id')} uploaded a scan for {plot_name}", user_id=session.get('user_id'))
                return redirect(url_for('homepage'))
    return render_template('scan_new.html', error=error)

@app.route('/superadmin/settings', methods=['GET', 'POST'])
@role_required('superadmin')
def superadmin_settings():
    config = SystemConfig.query.first()
    if not config:
        config = SystemConfig(system_name='VISCANE', maintenance_mode=False)
        db.session.add(config)
        db.session.commit()

    if request.method == 'POST':
        model_message = None
        model_file = request.files.get('model_file')
        if model_file and model_file.filename:
            filename = secure_filename(model_file.filename)
            target_dir = os.path.join(app.root_path, 'model_updates')
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            model_file.save(file_path)
            config.model_filename = filename
            model_message = f"Model '{filename}' uploaded successfully."
        system_name = request.form.get('system_name', '').strip() or config.system_name
        maintenance_mode = True if request.form.get('maintenance_mode') == 'on' else False
        config.system_name = system_name
        config.maintenance_mode = maintenance_mode
        db.session.commit()
        current_admin = get_current_admin()
        if current_admin:
            log_audit("System settings updated", user_id=current_admin.id)
            if model_message:
                log_audit(f"Model update received: {config.model_filename}", user_id=current_admin.id)
        if model_message:
            return redirect(url_for('superadmin_settings', success=model_message))
        return redirect(url_for('superadmin_settings'))

    return render_template(
        'superadmin_settings.html',
        config=config,
        success=request.args.get('success'),
        current_admin=get_current_admin()
    )

@app.route('/superadmin/reports')
@role_required('superadmin')
def superadmin_reports():
    logs = AgronomicLog.query.order_by(AgronomicLog.created_at.desc()).all()
    total_predictions = len(logs)
    rows = []
    total_lkg_tc = 0.0
    total_tc_ha = 0.0
    total_lkg = 0.0
    lkg_tc_count = 0
    tc_ha_count = 0
    for log in logs:
        if log.predicted_lkg_tc is not None:
            total_lkg_tc += float(log.predicted_lkg_tc)
            lkg_tc_count += 1
        if log.predicted_tc_ha is not None:
            total_tc_ha += float(log.predicted_tc_ha)
            tc_ha_count += 1
        if log.predicted_lkg is not None:
            total_lkg += float(log.predicted_lkg)
        rows.append({
            "farmer_name": log.user.fullname if log.user else f"User #{log.user_id}",
            "variety": log.variety or "N/A",
            "hectares": log.hectares or "N/A",
            "predicted_lkg_tc": round(log.predicted_lkg_tc, 2) if log.predicted_lkg_tc is not None else None,
            "predicted_lkg_ha": round(log.predicted_tc_ha, 2) if log.predicted_tc_ha is not None else None,
            "predicted_lkg": round(log.predicted_lkg, 2) if log.predicted_lkg is not None else None,
            "created_at": log.created_at
        })

    avg_lkg_tc = round(total_lkg_tc / lkg_tc_count, 2) if lkg_tc_count else 0
    avg_lkg_ha = round(total_tc_ha / tc_ha_count, 2) if tc_ha_count else 0
    total_estimated_lkg = round(total_lkg, 2) if total_predictions else 0

    report = {
        "avg_lkg_tc": avg_lkg_tc,
        "avg_lkg_ha": avg_lkg_ha,
        "total_estimated_lkg": total_estimated_lkg,
        "total_predictions": total_predictions,
    }

    return render_template(
        'superadmin_reports.html',
        report=report,
        rows=rows,
        current_admin=get_current_admin()
    )

@app.route('/superadmin/reports/download')
@role_required('superadmin')
def superadmin_reports_download():
    logs = AgronomicLog.query.order_by(AgronomicLog.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Prediction ID",
        "Farmer Name",
        "Variety",
        "Hectares",
        "Predicted LKG/TC",
        "Predicted LKG/HA",
        "Predicted Total LKG",
        "RSSI Infected",
        "Created At"
    ])
    for log in logs:
        farmer_name = log.user.fullname if log.user else f"User #{log.user_id}"
        writer.writerow([
            log.id,
            farmer_name,
            log.variety or "",
            log.hectares or "",
            round(log.predicted_lkg_tc, 2) if log.predicted_lkg_tc is not None else "",
            round(log.predicted_tc_ha, 2) if log.predicted_tc_ha is not None else "",
            round(log.predicted_lkg, 2) if log.predicted_lkg is not None else "",
            log.rssi_infected or "",
            log.created_at.strftime('%Y-%m-%d %H:%M:%S')
        ])

    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=superadmin_report.csv'
    return response

@app.route('/superadmin/audit')
@role_required('superadmin')
def superadmin_audit():
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(20).all()
    return render_template('superadmin_audit.html', logs=logs, current_admin=get_current_admin())

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
