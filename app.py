import os
import csv
import secrets
from copy import deepcopy
from datetime import datetime, timedelta
from functools import wraps
from io import StringIO
from shutil import disk_usage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, render_template, request, redirect, url_for, session, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from models import db, User, Admin, Scan, AuditLog, SystemConfig, Notification, Feedback, AgronomicLog

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
VARIETY_ALIASES = {
    "Mauritius RC888": "MAURITIO RC888",
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

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('VISCANE_SECRET_KEY', 'change-this-key')

database_url = (
    os.getenv('SQLALCHEMY_DATABASE_URI')
    or os.getenv('DATABASE_URL')
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

def get_variety_weights(variety, custom_weights=None):
    weights = deepcopy(DEFAULT_VARIETY_WEIGHTS)
    if custom_weights is not None:
        weights[variety] = custom_weights
    return weights.get(variety, weights["VMC 84-524"])

def compute_visual_grade(visual_features):
    if not visual_features:
        raise ValueError("visual_features must not be empty.")
    return sum(visual_features) / len(visual_features)

def compute_agronomic_adjustment(agronomic_input, weights):
    return sum(float(agronomic_input[key]) * weights[key] for key in AGRONOMIC_KEYS)

def compute_agronomic_penalty(agronomic_input, weights):
    contributions = [float(agronomic_input[key]) * weights[key] for key in AGRONOMIC_KEYS]
    return sum(value for value in contributions if value < 0)

def compute_agronomic_multiplier(agronomic_adjustment):
    return max(0.0, 1.0 + agronomic_adjustment)

def get_sra_baseline(variety, crop_stage):
    try:
        return SRA_BASELINE_LKG_TC[variety][crop_stage], SRA_BASELINE_TC_HA[variety][crop_stage]
    except KeyError as exc:
        raise ValueError("Missing SRA baseline for the selected variety or ratoon stage.") from exc

def predict_variety_metrics(variety, hectares, visual_features, agronomic_input, custom_weights=None):
    normalized_variety = normalize_variety_name(variety)
    if normalized_variety not in DEFAULT_VARIETY_WEIGHTS and custom_weights is None:
        raise ValueError("Unknown variety. Provide a known variety or include custom_weights.")

    crop_stage = int(round(float(agronomic_input["ratoon"])))
    if crop_stage not in CROP_STAGE_LABELS:
        raise ValueError("ratoon stage must be 1, 2, or 3.")

    weights_used = get_variety_weights(normalized_variety, custom_weights)
    visual_grade = compute_visual_grade(visual_features)
    agronomic_adjustment = compute_agronomic_adjustment(agronomic_input, weights_used)
    agronomic_penalty = compute_agronomic_penalty(agronomic_input, weights_used)
    agronomic_multiplier = compute_agronomic_multiplier(agronomic_penalty)
    baseline_lkg_tc, baseline_tc_ha_per_hectare = get_sra_baseline(normalized_variety, crop_stage)
    adjusted_baseline_tc_ha = baseline_tc_ha_per_hectare * hectares
    predicted_quality_grade = visual_grade + agronomic_adjustment
    raw_predicted_lkg_tc = baseline_lkg_tc * agronomic_multiplier
    predicted_lkg_tc = max(0.0, min(baseline_lkg_tc, raw_predicted_lkg_tc))
    raw_predicted_tc_ha = adjusted_baseline_tc_ha * agronomic_multiplier
    predicted_tc_ha = max(0.0, min(adjusted_baseline_tc_ha, raw_predicted_tc_ha))
    predicted_lkg = predicted_lkg_tc * predicted_tc_ha

    return {
        "variety": normalized_variety,
        "crop_stage": CROP_STAGE_LABELS[crop_stage],
        "hectares": hectares,
        "visual_grade": visual_grade,
        "agronomic_adjustment": agronomic_adjustment,
        "agronomic_penalty": agronomic_penalty,
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
        "input": {
            "hectares": hectares,
            "visual_features": visual_features,
            "agronomic_input": agronomic_input,
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

    if not Scan.query.filter_by(user_id=user.id).first():
        sample_scans = [
            Scan(user_id=user.id, plot_name='Plot #4 Sample', grade='A', maturity_pct=91, status='ready', created_at=datetime.utcnow() - timedelta(hours=2)),
            Scan(user_id=user.id, plot_name='Plot #2 Sample', grade='B', maturity_pct=76, status='monitor', created_at=datetime.utcnow() - timedelta(hours=3)),
            Scan(user_id=user.id, plot_name='Plot #1 Sample', grade='A', maturity_pct=88, status='healthy', created_at=datetime.utcnow() - timedelta(days=1)),
        ]
        db.session.add_all(sample_scans)
        db.session.commit()

    today = datetime.utcnow().date()
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    scans_today = Scan.query.filter(Scan.user_id == user.id, Scan.created_at >= datetime(today.year, today.month, today.day)).count()
    pending_scans = Scan.query.filter(Scan.user_id == user.id, Scan.status == 'pending').count()
    scans_last7 = Scan.query.filter(Scan.user_id == user.id, Scan.created_at >= seven_days_ago).all()
    recent_scans = Scan.query.filter_by(user_id=user.id).order_by(Scan.created_at.desc()).limit(3).all()
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
        agronomic_logs=agronomic_logs,
        announcements=announcements,
        recommendations=recommendations,
        grouped_recommendations=grouped_recommendations,
        message=message,
        error=error
    )

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

    latest_scan = Scan.query.filter_by(user_id=user.id).order_by(Scan.created_at.desc()).first()
    maturity_pct = latest_scan.maturity_pct if latest_scan else None

    visual_features = [0.21, 0.48, 0.63, 0.74, 0.59]
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
        }
    }

    has_complete_payload = (
        bool(variety)
        and hectares_value is not None
        and len(visual_features) == 5
        and all(isinstance(value, (int, float)) for value in visual_features)
        and all(value is not None for value in payload["agronomic_input"].values())
    )

    if has_complete_payload:
        try:
            prediction_response = predict_variety_metrics(
                variety=payload["variety"],
                hectares=payload["hectares"],
                visual_features=payload["visual_features"],
                agronomic_input=payload["agronomic_input"],
                custom_weights=payload["custom_weights"],
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

            if not email or not phone:
                error = 'Please complete your email and phone number.'
            elif len(phone) != 11 or not phone.isdigit():
                error = 'Phone number must be exactly 11 digits.'
            else:
                existing_user = User.query.filter(User.email == email, User.id != user.id).first()
                if existing_user:
                    error = 'Email already exists.'
                else:
                    user.email = email
                    user.phone = phone
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
    users = User.query.filter_by(is_archived=False, is_active=True).order_by(User.id.desc()).limit(6).all()
    logs = [
        {"icon": "server-outline", "title": "Database Backup", "meta": "Completed 1 hour ago", "status": "Success", "color": "#2E7D32"},
        {"icon": "warning-outline", "title": "Failed Login Attempt", "meta": "IP: 192.168.1.45 | 2 hrs ago", "status": "Alert", "color": "#C62828"},
        {"icon": "person-add-outline", "title": "New User Registration", "meta": "Maria Santos | 4 hrs ago", "status": "Review", "color": "#1565C0"},
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
        "model_accuracy": model_accuracy,
        "storage_utilization": storage_utilization,
    }
    return render_template(
        'admin.html',
        total_users=total_users,
        users=users,
        logs=logs,
        current_admin=current_admin,
        stats=stats
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
    scans = Scan.query.order_by(Scan.created_at.desc()).limit(50).all()
    monitoring_rows = []
    for scan in scans:
        tch, lkg_tc, trash_pct = estimate_scan_metrics(scan)
        bags = round(tch * 20, 2)
        monitoring_rows.append({
            "plot_name": scan.plot_name,
            "grade": scan.grade,
            "maturity_pct": scan.maturity_pct,
            "status": scan.status,
            "tch": tch,
            "lkg_tc": lkg_tc,
            "bags": bags,
            "created_at": scan.created_at
        })
    return render_template('admin_monitoring.html', rows=monitoring_rows, current_admin=get_current_admin())

@app.route('/admin/models', methods=['GET', 'POST'])
@login_required
def admin_models():
    config = get_system_config()
    message = None
    if request.method == 'POST':
        model_file = request.files.get('model_file')
        if model_file and model_file.filename:
            filename = secure_filename(model_file.filename)
            target_dir = os.path.join(app.root_path, 'model_updates')
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            model_file.save(file_path)
            config.model_filename = filename
            db.session.commit()
            log_audit(f"Model update received: {filename}", user_id=get_current_admin().id if get_current_admin() else None)
            message = f"Model '{filename}' uploaded successfully."
    return render_template('admin_models.html', config=config, message=message, current_admin=get_current_admin())

@app.route('/admin/reports')
@login_required
def admin_reports():
    scans = Scan.query.order_by(Scan.created_at.desc()).all()
    farmer_summary = {}
    for scan in scans:
        tch, lkg_tc, trash_pct = estimate_scan_metrics(scan)
        entry = farmer_summary.setdefault(scan.user_id, {
            "count": 0,
            "total_maturity": 0,
            "total_tch": 0,
            "total_lkg_tc": 0,
        })
        entry["count"] += 1
        entry["total_maturity"] += scan.maturity_pct
        entry["total_tch"] += tch
        entry["total_lkg_tc"] += lkg_tc
        entry["total_trash"] += trash_pct

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
            "scans": count,
            "avg_maturity": round(summary["total_maturity"] / count, 1) if count else 0,
            "avg_tch": round(summary["total_tch"] / count, 2) if count else 0,
            "avg_lkg_tc": round(summary["total_lkg_tc"] / count, 2) if count else 0,
            "avg_trash": round(summary["total_trash"] / count, 2) if count else 0
        })

    rows = sorted(rows, key=lambda item: item["scans"], reverse=True)
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
    return render_template('admin_login.html', error=error)

@app.route('/superadmin-login', methods=['GET', 'POST'])
def superadmin_login():
    if not Admin.query.filter_by(is_archived=False).first():
        return redirect(url_for('admin_setup'))
    error = None
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
    return render_template('superadmin_login.html', error=error)

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
    pending_scans = Scan.query.filter(
        Scan.status == 'pending',
        Scan.user_id.in_(active_user_ids)
    ).count()
    admins = Admin.query.filter_by(is_archived=False).order_by(Admin.id.desc()).all()
    users = User.query.filter_by(is_archived=False, is_active=True).order_by(User.id.desc()).limit(8).all()
    archived_users = User.query.filter_by(is_archived=True).order_by(User.id.desc()).all()
    deactivated_users = User.query.filter_by(is_archived=False, is_active=False).order_by(User.id.desc()).all()
    recent_scans = Scan.query.filter(Scan.user_id.in_(active_user_ids)).order_by(Scan.created_at.desc()).limit(6).all()
    return render_template(
        'superadmin.html',
        total_users=total_users,
        active_user_count=active_user_count,
        deactivated_user_count=deactivated_user_count,
        archived_user_count=archived_user_count,
        total_admins=total_admins,
        active_farmers=active_farmers,
        total_scans=total_scans,
        pending_scans=pending_scans,
        admins=admins,
        users=users,
        archived_users=archived_users,
        deactivated_users=deactivated_users,
        recent_scans=recent_scans,
        current_admin=get_current_admin()
    )

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
        system_name = request.form.get('system_name', '').strip() or config.system_name
        maintenance_mode = True if request.form.get('maintenance_mode') == 'on' else False
        config.system_name = system_name
        config.maintenance_mode = maintenance_mode
        db.session.commit()
        current_admin = get_current_admin()
        if current_admin:
            log_audit("System settings updated", user_id=current_admin.id)
        return redirect(url_for('superadmin_settings'))

    return render_template('superadmin_settings.html', config=config, current_admin=get_current_admin())

@app.route('/superadmin/reports')
@role_required('superadmin')
def superadmin_reports():
    scans = Scan.query.order_by(Scan.created_at.desc()).all()
    total_scans = len(scans)
    rows = []
    total_tch = 0
    total_lkg_tc = 0
    total_trash = 0
    for scan in scans:
        tch, lkg_tc, trash_pct = estimate_scan_metrics(scan)
        total_tch += tch
        total_lkg_tc += lkg_tc
        total_trash += trash_pct
        rows.append({
            "plot_name": scan.plot_name,
            "grade": scan.grade,
            "maturity_pct": scan.maturity_pct,
            "tch": tch,
            "lkg_tc": lkg_tc,
            "created_at": scan.created_at
        })

    avg_lkg_tc = round(total_lkg_tc / total_scans, 2) if total_scans else 0
    avg_trash_pct = round(total_trash / total_scans, 2) if total_scans else 0
    total_predicted_yield = round(total_tch, 2) if total_scans else 0

    report = {
        "avg_lkg_tc": avg_lkg_tc,
        "total_predicted_yield": total_predicted_yield,
        "total_scans": total_scans
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
    scans = Scan.query.order_by(Scan.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Scan ID",
        "Plot Name",
        "Grade",
        "Maturity %",
        "Estimated TCH",
        "Estimated LKG/TC",
        "Created At"
    ])
    for scan in scans:
        tch, lkg_tc, trash_pct = estimate_scan_metrics(scan)
        writer.writerow([
            scan.id,
            scan.plot_name,
            scan.grade,
            scan.maturity_pct,
            tch,
            lkg_tc,
            trash_pct,
            scan.created_at.strftime('%Y-%m-%d %H:%M:%S')
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
