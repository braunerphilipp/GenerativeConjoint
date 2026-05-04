"""
GenerativeConjoint — Flask application.
"""

import base64
import csv
import io
import json
import os
import secrets
import statistics
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode, urlparse, urljoin

import requests
from flask import (Flask, abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, session, url_for)

from config import Config
from design import generate_design, estimate_sample_size
from models import (Attribute, DesignTask, GeneratedImage, GeneratedText,
                    Level, Participant, PendingInvite, Response, Survey,
                    SurveyUser, User, db, _gen_public_id)

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)


# ---------------------------------------------------------------------------
# Bootstrap DB
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()
    # Runtime migrations for columns added after initial schema
    from sqlalchemy import inspect as _sa_inspect, text as _text
    def _add_col(table, col, typedef):
        cols = [c['name'] for c in _sa_inspect(db.engine).get_columns(table)]
        if col not in cols:
            db.session.execute(_text(f'ALTER TABLE {table} ADD COLUMN {col} {typedef}'))
            db.session.commit()
    _add_col('survey', 'public_id', 'VARCHAR(8)')
    _add_col('survey', 'language', "VARCHAR(100) DEFAULT 'English'")
    _add_col('survey', 'intro_text', 'TEXT')
    _add_col('survey', 'auto_advance', 'BOOLEAN DEFAULT 0')
    _add_col('survey', 'text_prompt', 'TEXT')
    _add_col('survey', 'texts_generated', 'BOOLEAN DEFAULT 0')
    _add_col('survey', 'image_prompt', 'TEXT')
    _add_col('survey', 'images_generated', 'BOOLEAN DEFAULT 0')
    _add_col('survey', 'pool_size', 'INTEGER')
    _add_col('survey', 'has_duplicate_profiles', 'BOOLEAN DEFAULT 0')
    _add_col('survey', 'duplicate_profile_count', 'INTEGER DEFAULT 0')
    _add_col('survey', 'design_stale', 'BOOLEAN DEFAULT 0')
    _add_col('level', 'llm_hint', 'TEXT')
    _add_col('generated_text', 'prompt', 'TEXT')
    _add_col('generated_text', 'profile_key', 'VARCHAR(500)')
    _add_col('generated_image', 'prompt', 'TEXT')
    _add_col('generated_image', 'profile_key', 'VARCHAR(500)')
    _add_col('participant', 'task_assignment', 'TEXT')
    _add_col('participant', 'prolific_pid', 'VARCHAR(500)')
    _add_col('participant', 'study_id', 'VARCHAR(500)')
    _add_col('participant', 'session_id', 'VARCHAR(500)')
    _add_col('survey', 'randomise_attrs', 'BOOLEAN DEFAULT 0')
    _add_col('response', 'response_time_ms', 'INTEGER')
    _add_col('survey', 'attr_order_mode', "VARCHAR(20) DEFAULT 'none'")
    # Backfill: old boolean True → 'task' mode
    db.session.execute(_text(
        "UPDATE survey SET attr_order_mode = 'task'"
        " WHERE randomise_attrs = 1 AND (attr_order_mode IS NULL OR attr_order_mode = 'none')"
    ))
    db.session.commit()
    _add_col('survey', 'image_model', "VARCHAR(50) DEFAULT 'gpt-image-2'")
    _add_col('survey', 'image_quality', "VARCHAR(20) DEFAULT 'medium'")
    _add_col('survey', 'show_intro', 'BOOLEAN DEFAULT 1')
    _add_col('survey', 'results_token', 'VARCHAR(24)')
    # Backfill results_token for existing surveys
    for _s in Survey.query.filter(Survey.results_token.is_(None)).all():
        _s.results_token = secrets.token_urlsafe(16)
    db.session.commit()
    # Backfill any rows that still have NULL public_id
    for _s in Survey.query.filter(Survey.public_id.is_(None)).all():
        _s.public_id = _gen_public_id()
    db.session.commit()

    # Migrate generated_text and generated_image to profile_key-based uniqueness
    with db.engine.connect() as _conn:
        for _tbl, _old_idx, _new_idx, _ddl in [
            ('generated_text', 'uq_generated_text', 'uq_generated_text_profile',
             """CREATE TABLE generated_text_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    survey_id INTEGER NOT NULL REFERENCES survey(id),
                    task_index INTEGER,
                    alternative_index INTEGER,
                    text TEXT NOT NULL,
                    prompt TEXT,
                    profile_key VARCHAR(500) NOT NULL,
                    generated_at DATETIME,
                    CONSTRAINT uq_generated_text_profile UNIQUE (survey_id, profile_key)
                )"""),
            ('generated_image', 'uq_generated_image', 'uq_generated_image_profile',
             """CREATE TABLE generated_image_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    survey_id INTEGER NOT NULL REFERENCES survey(id),
                    task_index INTEGER,
                    alternative_index INTEGER,
                    image_data BLOB NOT NULL,
                    thumb_data BLOB NOT NULL,
                    prompt TEXT,
                    profile_key VARCHAR(500) NOT NULL,
                    generated_at DATETIME,
                    CONSTRAINT uq_generated_image_profile UNIQUE (survey_id, profile_key)
                )"""),
        ]:
            _old_exists = _conn.execute(_text(
                f"SELECT name FROM sqlite_master WHERE type='index' AND name='{_old_idx}'"
            )).fetchone()
            if _old_exists:
                _conn.execute(_text(_ddl))
                _cols = 'id, survey_id, task_index, alternative_index, text, prompt, profile_key, generated_at' \
                    if _tbl == 'generated_text' else \
                    'id, survey_id, task_index, alternative_index, image_data, thumb_data, prompt, profile_key, generated_at'
                _conn.execute(_text(
                    f"INSERT OR IGNORE INTO {_tbl}_new ({_cols}) "
                    f"SELECT {_cols} FROM {_tbl} WHERE profile_key IS NOT NULL ORDER BY id"
                ))
                _conn.execute(_text(f"DROP TABLE {_tbl}"))
                _conn.execute(_text(f"ALTER TABLE {_tbl}_new RENAME TO {_tbl}"))
                _conn.commit()


# ---------------------------------------------------------------------------
# Global request context — always resolves current user for templates
# ---------------------------------------------------------------------------

@app.before_request
def _load_logged_in_user():
    g.current_user = None
    if 'user_id' in session:
        g.current_user = db.session.get(User, session['user_id'])


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        g.current_user = User.query.get(session['user_id'])
        if g.current_user is None:
            session.clear()
            flash('Session expired. Please log in again.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def survey_access_required(role_needed=None):
    """Decorator factory that validates survey membership."""
    def decorator(f):
        @wraps(f)
        def decorated(survey_id, *args, **kwargs):
            if 'user_id' not in session:
                flash('Please log in first.', 'warning')
                return redirect(url_for('login'))
            g.current_user = User.query.get(session['user_id'])
            if g.current_user is None:
                session.clear()
                flash('Session expired. Please log in again.', 'warning')
                return redirect(url_for('login'))
            survey = Survey.query.get_or_404(survey_id)
            membership = SurveyUser.query.filter_by(
                survey_id=survey_id, user_id=g.current_user.id,
                status='active'
            ).first()
            if not membership:
                abort(403)
            if role_needed == 'owner' and membership.role != 'owner':
                abort(403)
            g.survey = survey
            g.membership = membership
            return f(survey_id, *args, **kwargs)
        return decorated
    return decorator


# ---------------------------------------------------------------------------
# ORCID OAuth
# ---------------------------------------------------------------------------

@app.route('/login')
def login():
    if 'user_id' in session:
        return redirect(url_for('surveys_list'))
    return render_template('auth/login.html')


@app.route('/auth/orcid')
def auth_orcid():
    if not app.config['ORCID_CLIENT_ID']:
        flash('ORCID credentials are not configured. Add them to your .env file.', 'danger')
        return redirect(url_for('login'))
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = {
        'client_id': app.config['ORCID_CLIENT_ID'],
        'response_type': 'code',
        'scope': '/authenticate',
        'redirect_uri': app.config['ORCID_REDIRECT_URI'],
        'state': state,
    }
    auth_url = f"{app.config['ORCID_BASE_URL']}/oauth/authorize?{urlencode(params)}"
    return redirect(auth_url)


@app.route('/auth/callback')
def auth_callback():
    if request.args.get('state') != session.pop('oauth_state', None):
        flash('OAuth state mismatch — possible CSRF.', 'danger')
        return redirect(url_for('login'))

    code = request.args.get('code')
    if not code:
        flash('ORCID did not return an authorisation code.', 'danger')
        return redirect(url_for('login'))

    token_resp = requests.post(
        f"{app.config['ORCID_BASE_URL']}/oauth/token",
        data={
            'client_id': app.config['ORCID_CLIENT_ID'],
            'client_secret': app.config['ORCID_CLIENT_SECRET'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': app.config['ORCID_REDIRECT_URI'],
        },
        headers={'Accept': 'application/json'},
        timeout=10,
    )

    if token_resp.status_code != 200:
        flash('Failed to obtain access token from ORCID.', 'danger')
        return redirect(url_for('login'))

    token_data = token_resp.json()
    orcid_id = token_data.get('orcid')
    name = token_data.get('name', '')  # fallback from token

    if not orcid_id:
        flash('ORCID did not return a valid ID.', 'danger')
        return redirect(url_for('login'))

    # Fetch full name from the ORCID public API (more reliable than the token field)
    try:
        pd_resp = requests.get(
            f"{app.config['ORCID_API_URL']}/v3.0/{orcid_id}/personal-details",
            headers={'Accept': 'application/json'},
            timeout=5,
        )
        if pd_resp.status_code == 200:
            pd = pd_resp.json()
            name_block = pd.get('name') or {}
            credit = (name_block.get('credit-name') or {}).get('value', '')
            given = (name_block.get('given-names') or {}).get('value', '')
            family = (name_block.get('family-name') or {}).get('value', '')
            name = credit or f"{given} {family}".strip() or name
    except Exception:
        pass  # keep name from token

    user = User.query.filter_by(orcid=orcid_id).first()
    if not user:
        user = User(orcid=orcid_id, name=name)
        db.session.add(user)
        db.session.flush()
    elif name and user.name != name:
        user.name = name

    # Redeem any pending invites for this ORCID
    pending = PendingInvite.query.filter_by(orcid=orcid_id).all()
    for invite in pending:
        existing = SurveyUser.query.filter_by(
            survey_id=invite.survey_id, user_id=user.id
        ).first()
        if existing:
            existing.status = 'active'
            existing.role = 'collaborator'
        else:
            db.session.add(SurveyUser(
                survey_id=invite.survey_id, user_id=user.id,
                role='collaborator', status='active',
            ))
        db.session.delete(invite)
    db.session.commit()

    session['user_id'] = user.id
    flash(f'Welcome, {user.name or orcid_id}!', 'success')
    return redirect(url_for('surveys_list'))


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Survey list
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def surveys_list():
    user = g.current_user
    active = (
        db.session.query(Survey)
        .join(SurveyUser)
        .filter(SurveyUser.user_id == user.id, SurveyUser.status == 'active')
        .order_by(Survey.created_at.desc())
        .all()
    )
    archived = (
        db.session.query(Survey)
        .join(SurveyUser)
        .filter(SurveyUser.user_id == user.id, SurveyUser.status == 'archived')
        .order_by(Survey.created_at.desc())
        .all()
    )
    return render_template('surveys/list.html', active=active, archived=archived)


# ---------------------------------------------------------------------------
# Survey creation
# ---------------------------------------------------------------------------

@app.route('/surveys/new', methods=['GET', 'POST'])
@login_required
def surveys_new():
    if request.method == 'POST':
        return _save_survey(survey=None)
    return render_template('surveys/create.html', survey=None)


@app.route('/surveys/<int:survey_id>/edit', methods=['GET', 'POST'])
@survey_access_required(role_needed='owner')
def surveys_edit(survey_id):
    survey = g.survey
    if request.method == 'POST':
        return _save_survey(survey=survey)
    return render_template('surveys/create.html', survey=survey)


def _clear_generated_texts(survey):
    GeneratedText.query.filter_by(survey_id=survey.id).delete()
    survey.texts_generated = False


def _clear_generated_images(survey):
    GeneratedImage.query.filter_by(survey_id=survey.id).delete()
    survey.images_generated = False


def _save_survey(survey):
    """Parse the JSON payload posted by the form and persist the survey."""
    raw = request.form.get('form_data', '')
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        flash('Invalid form data. Please try again.', 'danger')
        return redirect(request.referrer or url_for('surveys_new'))

    title = data.get('title', '').strip()
    if not title:
        flash('Survey title is required.', 'danger')
        return redirect(request.referrer or url_for('surveys_new'))

    attributes_data = data.get('attributes', [])
    if not (1 <= len(attributes_data) <= 6):
        flash('You must define between 1 and 6 attributes.', 'danger')
        return redirect(request.referrer or url_for('surveys_new'))

    for a in attributes_data:
        if not (2 <= len(a.get('levels', [])) <= 4):
            flash(f'Attribute "{a.get("name")}" must have 2–4 levels.', 'danger')
            return redirect(request.referrer or url_for('surveys_new'))

    is_new = survey is None
    if is_new:
        survey = Survey(
            created_by=g.current_user.id,
            results_token=secrets.token_urlsafe(16),
        )
        db.session.add(survey)

    survey.title = title
    survey.language = data.get('language', 'English').strip() or 'English'
    survey.description = data.get('description', '').strip()
    survey.intro_text = data.get('intro_text', '').strip() or None
    survey.show_intro = bool(data.get('show_intro', True))
    survey.auto_advance = bool(data.get('auto_advance', False))
    survey.attr_order_mode = data.get('attr_order_mode', 'none')
    survey.presentation_type = data.get('presentation_type', 'tabular')
    survey.num_tasks = int(data.get('num_tasks', 8))
    survey.num_alternatives = int(data.get('num_alternatives', 2))
    num_blocks_raw = data.get('num_blocks')
    if num_blocks_raw:
        nb = int(num_blocks_raw)
        survey.pool_size = nb * survey.num_tasks if nb >= 2 else None
    else:
        survey.pool_size = None
    # redirect_url is managed from the detail page; only update if explicitly sent
    if 'redirect_url' in data:
        survey.redirect_url = data.get('redirect_url', '').strip()
    survey.text_prompt = data.get('text_prompt', '').strip() or None
    survey.image_prompt = data.get('image_prompt', '').strip() or None
    survey.image_model = data.get('image_model', 'gpt-image-2') or 'gpt-image-2'
    survey.image_quality = data.get('image_quality', 'medium') or 'medium'

    # Replace attributes and levels wholesale
    if not is_new:
        for attr in survey.attributes:
            db.session.delete(attr)
        db.session.flush()

    for idx, a_data in enumerate(attributes_data):
        attr = Attribute(
            survey=survey,
            name=a_data.get('name', '').strip(),
            description=a_data.get('description', '').strip(),
            order_index=idx,
        )
        db.session.add(attr)
        db.session.flush()
        for l_idx, l_data in enumerate(a_data.get('levels', [])):
            level = Level(
                attribute_id=attr.id,
                name=l_data.get('name', '').strip(),
                description=l_data.get('description', '').strip(),
                llm_hint=l_data.get('llm_hint', '').strip() or None,
                order_index=l_idx,
            )
            db.session.add(level)

    db.session.flush()

    # Attribute/level changes invalidate previously generated stimuli and the design
    _clear_generated_texts(survey)
    _clear_generated_images(survey)
    survey.design_stale = True

    if is_new:
        membership = SurveyUser(
            survey=survey, user=g.current_user, role='owner', status='active'
        )
        db.session.add(membership)

    db.session.commit()
    flash('Survey saved. Generate the design from the survey page.', 'info')
    return redirect(url_for('surveys_detail', survey_id=survey.id))


# ---------------------------------------------------------------------------
# Survey detail
# ---------------------------------------------------------------------------

def _compute_part_worths(survey_id, attributes):
    """Attribute relative importance via level win-rate method (preliminary, no MNL model)."""
    if not attributes:
        return None
    responses = (
        db.session.query(Response)
        .join(Participant, Response.participant_id == Participant.id)
        .filter(Participant.survey_id == survey_id)
        .filter(Response.choice_set.isnot(None))
        .all()
    )
    if len(responses) < 5:
        return None
    shown = {}
    won = {}
    for resp in responses:
        try:
            choice_set = json.loads(resp.choice_set)
        except (json.JSONDecodeError, TypeError):
            continue
        for alt_idx, profile in enumerate(choice_set):
            for attr_id_str, level_id_str in profile.items():
                key = (int(attr_id_str), int(level_id_str))
                shown[key] = shown.get(key, 0) + 1
                if alt_idx == resp.chosen_alternative:
                    won[key] = won.get(key, 0) + 1
    attr_data = []
    for attr in attributes:
        level_rates = {}
        for lv in attr.levels:
            key = (attr.id, lv.id)
            if key in shown:
                level_rates[lv.id] = won.get(key, 0) / shown[key]
        if not level_rates:
            attr_data.append({'attr': attr, 'range': 0.0, 'level_rates': {}})
            continue
        min_rate = min(level_rates.values())
        max_rate = max(level_rates.values())
        attr_data.append({
            'attr': attr,
            'range': max_rate - min_rate if len(level_rates) >= 2 else 0.0,
            'level_rates': level_rates,
            'min_rate': min_rate,
            'max_rate': max_rate,
        })
    total = sum(d['range'] for d in attr_data)
    if total == 0:
        return None
    result = []
    for d in attr_data:
        importance = round(d['range'] / total * 100, 1)
        # Per-level utilities: center around 0 within attribute (deviation from mean)
        rates = d['level_rates']
        if rates:
            mean_rate = sum(rates.values()) / len(rates)
            attr_range = d['range'] if d['range'] > 0 else 1.0
            levels = [
                {
                    'name': lv.name,
                    'utility': round((rates.get(lv.id, mean_rate) - mean_rate) / attr_range * 100, 1),
                    'n_shown': shown.get((d['attr'].id, lv.id), 0),
                }
                for lv in d['attr'].levels
            ]
        else:
            levels = [{'name': lv.name, 'utility': 0.0, 'n_shown': 0} for lv in d['attr'].levels]
        result.append({'name': d['attr'].name, 'importance': importance, 'levels': levels})
    return sorted(result, key=lambda x: x['importance'], reverse=True)


@app.route('/surveys/<int:survey_id>')
@survey_access_required()
def surveys_detail(survey_id):
    survey = g.survey
    attributes = survey.attributes
    participant_count = survey.participants.count()
    completed_count = survey.participants.filter(
        Participant.completed_at.isnot(None)
    ).count()
    max_levels = max((len(a.levels) for a in attributes), default=2)
    min_n, rec_n = estimate_sample_size(survey.num_tasks, survey.num_alternatives, max_levels)

    base_url = request.host_url.rstrip('/')
    entry_url = f"{base_url}/s/{survey.public_id}"
    collaborators = (
        db.session.query(SurveyUser, User)
        .join(User, SurveyUser.user_id == User.id)
        .filter(SurveyUser.survey_id == survey_id, SurveyUser.status == 'active')
        .all()
    )
    design_tasks = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks
    num_blocks = (survey.pool_size // survey.num_tasks) if (survey.pool_size and survey.pool_size > survey.num_tasks) else 1

    if survey.design_generated and survey.presentation_type in ('textual', 'visual'):
        pool_cells = DesignTask.query.filter(
            DesignTask.survey_id == survey_id,
            DesignTask.task_index < design_tasks,
        ).all()
        unique_profiles = {_profile_key(json.loads(c.profile)) for c in pool_cells}
        if survey.presentation_type == 'textual':
            gen_pks = {gt.profile_key for gt in GeneratedText.query.filter_by(survey_id=survey_id)}
        else:
            gen_pks = {gi.profile_key for gi in GeneratedImage.query.filter_by(survey_id=survey_id)}
        missing_stimuli = max(0, len(unique_profiles) - len(gen_pks & unique_profiles))
    else:
        missing_stimuli = 0

    part_worths = _compute_part_worths(survey_id, attributes)
    pending_invites = PendingInvite.query.filter_by(survey_id=survey_id).all() if g.membership.role == 'owner' else []

    return render_template(
        'surveys/detail.html',
        survey=survey,
        attributes=attributes,
        participant_count=participant_count,
        completed_count=completed_count,
        entry_url=entry_url,
        collaborators=collaborators,
        is_owner=(g.membership.role == 'owner'),
        min_n=min_n,
        rec_n=rec_n,
        design_tasks=design_tasks,
        num_blocks=num_blocks,
        missing_stimuli=missing_stimuli,
        part_worths=part_worths,
        pending_invites=pending_invites,
    )


# ---------------------------------------------------------------------------
# Survey results page
# ---------------------------------------------------------------------------

def _fmt_duration(seconds):
    """Format seconds as 'Xm Ys'."""
    m, s = divmod(int(seconds), 60)
    return f'{m}m {s:02d}s' if m else f'{s}s'


def _duration_stats(completed_participants):
    """Compute duration statistics from a list of completed Participant rows."""
    durations = [
        (p.completed_at - p.started_at).total_seconds()
        for p in completed_participants
        if p.completed_at and p.started_at and p.completed_at > p.started_at
    ]
    if not durations:
        return None
    n = len(durations)
    # Quartiles require at least 4 data points for meaningful interpolation
    if n >= 4:
        q = statistics.quantiles(durations, n=4, method='inclusive')
        p25 = _fmt_duration(q[0])
        p75 = _fmt_duration(q[2])
    else:
        p25 = None
        p75 = None
    return {
        'mean':   _fmt_duration(statistics.mean(durations)),
        'median': _fmt_duration(statistics.median(durations)),
        'p25':    p25,
        'p75':    p75,
        'n':      n,
    }


def _results_context(survey):
    """Shared data assembly for both authenticated and public results views."""
    all_participants = survey.participants.all()
    participant_count = len(all_participants)
    completed = [p for p in all_participants if p.completed_at]

    # "Active" = incomplete participant who submitted a response in the last 60 s
    cutoff = datetime.utcnow() - timedelta(seconds=60)
    recent_pids = {
        r.participant_id
        for r in Response.query.join(Participant)
            .filter(
                Participant.survey_id == survey.id,
                Participant.completed_at.is_(None),
                Response.responded_at >= cutoff,
            )
    }
    active_count  = len(recent_pids)
    dropout_count = participant_count - len(completed) - active_count

    # First / last completion timestamps
    completed_sorted = sorted(completed, key=lambda p: p.completed_at)
    first_completed = completed_sorted[0].completed_at  if completed_sorted else None
    last_completed  = completed_sorted[-1].completed_at if completed_sorted else None

    return {
        'participant_count': participant_count,
        'completed_count':   len(completed),
        'active_count':      active_count,
        'dropout_count':     dropout_count,
        'first_completed':   first_completed.strftime('%Y-%m-%d %H:%M') if first_completed else None,
        'last_completed':    last_completed.strftime('%Y-%m-%d %H:%M')  if last_completed  else None,
        'dur_stats':         _duration_stats(completed),
        'part_worths':       _compute_part_worths(survey.id, survey.attributes),
    }


@app.route('/surveys/<int:survey_id>/inspect-design')
@survey_access_required()
def surveys_inspect_design(survey_id):
    survey = g.survey
    attributes = survey.attributes
    level_map = {lv.id: lv for a in attributes for lv in a.levels}
    design_tasks = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks
    num_blocks = (survey.pool_size // survey.num_tasks) if (survey.pool_size and survey.pool_size > survey.num_tasks) else 1

    # Build blocks: list of (block_idx, list of (task_idx, [alt_profiles]))
    blocks = []
    for b in range(num_blocks):
        start = b * survey.num_tasks
        end = start + survey.num_tasks
        tasks_in_block = []
        for t in range(start, end):
            alts = []
            for a_idx in range(survey.num_alternatives):
                cell = DesignTask.query.filter_by(survey_id=survey_id, task_index=t, alternative_index=a_idx).first()
                if cell:
                    profile = json.loads(cell.profile)
                    alts.append({attr.name: level_map.get(int(profile.get(str(attr.id), 0)), None) for attr in attributes})
                else:
                    alts.append({})
            tasks_in_block.append({'task_index': t, 'alts': alts})
        blocks.append({'block_index': b, 'tasks': tasks_in_block})

    # Build stimuli table — keyed by unique profile
    def _profile_vals(cell):
        if not cell:
            return {}
        profile = json.loads(cell.profile)
        return {attr.name: (level_map.get(int(profile.get(str(attr.id), 0))).name
                            if level_map.get(int(profile.get(str(attr.id), 0))) else '—')
                for attr in attributes}

    cell_by_pos = {
        (c.task_index, c.alternative_index): c
        for c in DesignTask.query.filter_by(survey_id=survey_id)
    }

    # Count how many positions share each profile_key (for "shared" indicator)
    from collections import Counter as _Counter
    pk_counts = _Counter(
        _profile_key(json.loads(cell_by_pos[(t, a)].profile))
        for t in range(design_tasks) for a in range(survey.num_alternatives)
        if (t, a) in cell_by_pos
    )

    if survey.presentation_type == 'textual':
        gt_map = {gt.profile_key: gt for gt in GeneratedText.query.filter_by(survey_id=survey_id)}
        stimuli = []
        for t in range(design_tasks):
            for a_idx in range(survey.num_alternatives):
                cell = cell_by_pos.get((t, a_idx))
                pk = _profile_key(json.loads(cell.profile)) if cell else None
                gt = gt_map.get(pk) if pk else None
                stimuli.append({'task': t, 'alt': a_idx, 'profile': _profile_vals(cell),
                                'generated': gt is not None,
                                'stimulus_id': gt.id if gt else None,
                                'content': gt.text if gt else None,
                                'profile_key': pk,
                                'shared': (pk_counts.get(pk, 1) > 1) if pk else False})
        stimulus_type = 'text'
    elif survey.presentation_type == 'visual':
        gi_map = {gi.profile_key: gi for gi in GeneratedImage.query.filter_by(survey_id=survey_id)}
        stimuli = []
        for t in range(design_tasks):
            for a_idx in range(survey.num_alternatives):
                cell = cell_by_pos.get((t, a_idx))
                pk = _profile_key(json.loads(cell.profile)) if cell else None
                gi = gi_map.get(pk) if pk else None
                stimuli.append({'task': t, 'alt': a_idx, 'profile': _profile_vals(cell),
                                'generated': gi is not None,
                                'stimulus_id': gi.id if gi else None,
                                'profile_key': pk,
                                'shared': (pk_counts.get(pk, 1) > 1) if pk else False})
        stimulus_type = 'image'
    else:
        stimuli = []
        stimulus_type = 'tabular'

    return render_template(
        'surveys/inspect_design.html',
        survey=survey,
        attributes=attributes,
        blocks=blocks,
        num_blocks=num_blocks,
        design_tasks=design_tasks,
        stimuli=stimuli,
        stimulus_type=stimulus_type,
        is_owner=(g.membership.role == 'owner'),
    )


@app.route('/surveys/<int:survey_id>/delete-text/<int:text_id>', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_delete_text(survey_id, text_id):
    gt = GeneratedText.query.filter_by(id=text_id, survey_id=survey_id).first_or_404()
    db.session.delete(gt)
    g.survey.texts_generated = GeneratedText.query.filter_by(survey_id=survey_id).count() > 0
    db.session.commit()
    return redirect(url_for('surveys_inspect_design', survey_id=survey_id) + '#stimuli')


@app.route('/surveys/<int:survey_id>/delete-image/<int:image_id>', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_delete_image(survey_id, image_id):
    gi = GeneratedImage.query.filter_by(id=image_id, survey_id=survey_id).first_or_404()
    db.session.delete(gi)
    g.survey.images_generated = GeneratedImage.query.filter_by(survey_id=survey_id).count() > 0
    db.session.commit()
    return redirect(url_for('surveys_inspect_design', survey_id=survey_id) + '#stimuli')


@app.route('/surveys/<int:survey_id>/results')
@survey_access_required()
def surveys_results(survey_id):
    survey = g.survey
    ctx = _results_context(survey)
    public_url = url_for(
        'surveys_results_public',
        slug=_slugify(survey.title) or 'results',
        token=survey.results_token,
        _external=True,
    )
    return render_template('surveys/results.html', survey=survey,
                           public=False, public_url=public_url, **ctx)


@app.route('/results/<slug>/<token>')
def surveys_results_public(slug, token):
    """Public results view — no login required; URL authenticated by results_token."""
    survey = Survey.query.filter_by(results_token=token).first_or_404()
    ctx = _results_context(survey)
    return render_template('surveys/results.html', survey=survey, public=True, **ctx)


# ---------------------------------------------------------------------------
# Survey actions
# ---------------------------------------------------------------------------

@app.route('/surveys/<int:survey_id>/regenerate', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_regenerate(survey_id):
    survey = g.survey
    _clear_generated_texts(survey)
    _clear_generated_images(survey)
    d_eff = _generate_and_store_design(survey)
    survey.d_efficiency = d_eff
    db.session.commit()
    flash(f'Design regenerated (D-efficiency: {d_eff:.4f}).', 'success')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/generate-design', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_generate_design(survey_id):
    survey = g.survey
    _clear_generated_texts(survey)
    _clear_generated_images(survey)
    d_eff = _generate_and_store_design(survey)
    survey.d_efficiency = d_eff
    db.session.commit()
    flash(f'Design generated (D-efficiency: {d_eff:.4f}).', 'success')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/clear-responses', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_clear_responses(survey_id):
    survey = g.survey
    for participant in survey.participants:
        db.session.delete(participant)
    db.session.commit()
    flash('All responses have been deleted.', 'warning')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/start-text-generation', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_start_text_generation(survey_id):
    """Clear existing texts and return the ordered generation plan as JSON."""
    survey = g.survey
    if not survey.design_generated:
        return jsonify(error='Design not generated yet.'), 400
    if survey.presentation_type != 'textual':
        return jsonify(error='Not a textual survey.'), 400
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured.'), 503
    if not (survey.text_prompt or '').strip():
        return jsonify(error='No text generation prompt configured. Edit the survey to add one.'), 400

    pool_size = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks
    cell_by_pos = {
        (c.task_index, c.alternative_index): c
        for c in DesignTask.query.filter_by(survey_id=survey_id)
    }
    existing_pks = {gt.profile_key for gt in GeneratedText.query.filter_by(survey_id=survey_id)}
    seen_pks = set()
    profiles = []
    for t in range(pool_size):
        for a in range(survey.num_alternatives):
            cell = cell_by_pos.get((t, a))
            if not cell:
                continue
            pk = _profile_key(json.loads(cell.profile))
            if pk not in existing_pks and pk not in seen_pks:
                seen_pks.add(pk)
                profiles.append({'task_index': t, 'alternative_index': a})
    return jsonify(profiles=profiles, total=len(profiles))


@app.route('/surveys/<int:survey_id>/generate-single-text', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_generate_single_text(survey_id):
    """Generate and store the text for one (task, alternative) profile."""
    survey = g.survey
    data = request.get_json(force=True) or {}
    task_index = data.get('task_index')
    alt_index = data.get('alternative_index')
    if task_index is None or alt_index is None:
        return jsonify(error='Missing task_index or alternative_index.'), 400

    cell = DesignTask.query.filter_by(
        survey_id=survey_id, task_index=task_index, alternative_index=alt_index
    ).first()
    if not cell:
        return jsonify(error=f'Profile not found (task {task_index}, alt {alt_index}).'), 404

    attributes = (Attribute.query.filter_by(survey_id=survey_id)
                  .order_by(Attribute.order_index).all())
    level_map = {lv.id: lv for a in attributes for lv in a.levels}

    profile = json.loads(cell.profile)
    pk = _profile_key(profile)

    # One stimulus per unique profile — return immediately if already generated
    if GeneratedText.query.filter_by(survey_id=survey_id, profile_key=pk).first():
        return jsonify(ok=True, cached=True)

    lines = []
    for attr in attributes:
        lv_id = profile.get(str(attr.id))
        if lv_id:
            lv = level_map.get(int(lv_id))
            if lv:
                entry = f'- {attr.name}: {lv.name}'
                if lv.llm_hint:
                    entry += f' ({lv.llm_hint})'
                lines.append(entry)
    full_prompt = f"{survey.text_prompt.strip()}\n\nProfile:\n{chr(10).join(lines)}"

    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])
        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write concise, engaging descriptions for survey stimuli. '
                        'Output only the requested text — no headings, no markdown, '
                        'no meta-commentary, no preamble.'
                    ),
                },
                {'role': 'user', 'content': full_prompt},
            ],
            temperature=0.7,
            max_tokens=400,
            service_tier='flex',
        )
        text = resp.choices[0].message.content.strip()
        db.session.add(GeneratedText(
            survey_id=survey_id, task_index=task_index, alternative_index=alt_index,
            text=text, prompt=full_prompt, profile_key=pk,
        ))
        db.session.commit()
        return jsonify(ok=True)

    except Exception as exc:
        app.logger.error('generate-single-text error: %s', exc)
        return jsonify(error=str(exc)), 500


@app.route('/surveys/<int:survey_id>/finalize-texts', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_finalize_texts(survey_id):
    """Mark all texts as generated after the JS loop completes."""
    g.survey.texts_generated = True
    db.session.commit()
    return jsonify(ok=True)


@app.route('/surveys/<int:survey_id>/clear-texts', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_clear_texts(survey_id):
    survey = g.survey
    _clear_generated_texts(survey)
    db.session.commit()
    flash('Generated texts cleared.', 'info')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


# ---------------------------------------------------------------------------
# Image serving (no auth — participants need access during task)
# ---------------------------------------------------------------------------

@app.route('/surveys/<int:survey_id>/images/<int:task_idx>/<int:alt_idx>/<size>')
def serve_image(survey_id, task_idx, alt_idx, size):
    cell = DesignTask.query.filter_by(
        survey_id=survey_id, task_index=task_idx, alternative_index=alt_idx
    ).first_or_404()
    pk = _profile_key(json.loads(cell.profile))
    img = GeneratedImage.query.filter_by(survey_id=survey_id, profile_key=pk).first_or_404()
    data = img.thumb_data if size == 'thumb' else img.image_data
    resp = make_response(data)
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


# ---------------------------------------------------------------------------
# Image generation (JS-driven, one profile at a time)
# ---------------------------------------------------------------------------

@app.route('/surveys/<int:survey_id>/start-image-generation', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_start_image_generation(survey_id):
    survey = g.survey
    if not survey.design_generated:
        return jsonify(error='Generate the design first.'), 400
    if survey.presentation_type != 'visual':
        return jsonify(error='Not a visual survey.'), 400
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured.'), 503
    if not (survey.image_prompt or '').strip():
        return jsonify(error='No image prompt configured. Edit the survey to add one.'), 400
    pool_size = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks
    cell_by_pos = {
        (c.task_index, c.alternative_index): c
        for c in DesignTask.query.filter_by(survey_id=survey_id)
    }
    existing_pks = {gi.profile_key for gi in GeneratedImage.query.filter_by(survey_id=survey_id)}
    seen_pks = set()
    profiles = []
    for t in range(pool_size):
        for a in range(survey.num_alternatives):
            cell = cell_by_pos.get((t, a))
            if not cell:
                continue
            pk = _profile_key(json.loads(cell.profile))
            if pk not in existing_pks and pk not in seen_pks:
                seen_pks.add(pk)
                profiles.append({'task_index': t, 'alternative_index': a})
    return jsonify(profiles=profiles, total=len(profiles))


@app.route('/surveys/<int:survey_id>/generate-single-image', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_generate_single_image(survey_id):
    survey = g.survey
    data = request.get_json(force=True) or {}
    task_index = data.get('task_index')
    alt_index = data.get('alternative_index')
    if task_index is None or alt_index is None:
        return jsonify(error='Missing task_index or alternative_index.'), 400

    cell = DesignTask.query.filter_by(
        survey_id=survey_id, task_index=task_index, alternative_index=alt_index
    ).first()
    if not cell:
        return jsonify(error='Profile not found.'), 404

    attributes = (Attribute.query.filter_by(survey_id=survey_id)
                  .order_by(Attribute.order_index).all())
    level_map = {lv.id: lv for a in attributes for lv in a.levels}

    profile = json.loads(cell.profile)
    pk = _profile_key(profile)

    # One image per unique profile — return immediately if already generated
    if GeneratedImage.query.filter_by(survey_id=survey_id, profile_key=pk).first():
        return jsonify(ok=True, cached=True)

    parts = []
    for attr in attributes:
        lv_id = profile.get(str(attr.id))
        if lv_id:
            lv = level_map.get(int(lv_id))
            if lv:
                entry = f'{attr.name}: {lv.name}'
                if lv.llm_hint:
                    entry += f' ({lv.llm_hint})'
                parts.append(entry)
    full_prompt = survey.image_prompt.strip()
    if parts:
        full_prompt += '. ' + ', '.join(parts) + '.'

    model = survey.image_model or 'gpt-image-2'
    quality = survey.image_quality or 'medium'
    full_prompt = full_prompt[:4000]

    try:
        from openai import OpenAI as _OpenAI
        from PIL import Image as _PILImage
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])
        if model == 'dall-e-3':
            # dall-e-3 only supports 'standard' and 'hd'
            dalle_quality = 'hd' if quality == 'high' else 'standard'
            resp = ai.images.generate(
                model='dall-e-3',
                prompt=full_prompt,
                size='1024x1024',
                quality=dalle_quality,
                response_format='b64_json',
                n=1,
            )
            image_bytes = base64.b64decode(resp.data[0].b64_json)
        else:
            # gpt-image-2 and gpt-image-2-mini support low/medium/high
            resp = ai.images.generate(
                model=model,
                prompt=full_prompt,
                size='1024x1024',
                quality=quality,
                n=1,
            )
            image_bytes = base64.b64decode(resp.data[0].b64_json)

        # Downscale to 400×400 for the participant thumbnail
        img_obj = _PILImage.open(io.BytesIO(image_bytes))
        thumb_obj = img_obj.resize((400, 400), _PILImage.LANCZOS)
        thumb_buf = io.BytesIO()
        thumb_obj.save(thumb_buf, format='PNG')
        thumb_bytes = thumb_buf.getvalue()

        db.session.add(GeneratedImage(
            survey_id=survey_id,
            task_index=task_index,
            alternative_index=alt_index,
            image_data=image_bytes,
            thumb_data=thumb_bytes,
            prompt=full_prompt,
            profile_key=pk,
        ))
        db.session.commit()
        return jsonify(ok=True)

    except Exception as exc:
        app.logger.error('generate-single-image error: %s', exc)
        return jsonify(error=str(exc)), 500


@app.route('/surveys/<int:survey_id>/finalize-images', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_finalize_images(survey_id):
    g.survey.images_generated = True
    db.session.commit()
    return jsonify(ok=True)


@app.route('/surveys/<int:survey_id>/clear-images', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_clear_images(survey_id):
    _clear_generated_images(g.survey)
    db.session.commit()
    flash('Generated images cleared.', 'info')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


# ---------------------------------------------------------------------------
# API: AI image prompt helpers
# ---------------------------------------------------------------------------

@app.route('/api/generate-text-prompt', methods=['POST'])
@login_required
def api_generate_text_prompt():
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured.'), 503
    data = request.get_json(force=True) or {}
    attributes = data.get('attributes', [])
    language = (data.get('language') or 'English').strip()
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    if not attributes:
        return jsonify(error='No attributes defined yet.'), 400

    attr_lines = []
    for a in attributes:
        levels = [lv.get('name', '') for lv in a.get('levels', []) if lv.get('name', '').strip()]
        hints  = [lv.get('llm_hint', '') for lv in a.get('levels', []) if lv.get('llm_hint', '').strip()]
        line = f"  - {a.get('name', '(unnamed)')}: {', '.join(levels)}"
        if hints:
            line += f"  (hints: {', '.join(hints)})"
        attr_lines.append(line)
    attr_block = '\n'.join(attr_lines)

    context_lines = []
    if title:
        context_lines.append(f'Survey title: {title}')
    if description:
        context_lines.append(f'Survey description: {description}')
    context_block = '\n'.join(context_lines)

    user_msg = (
        f"{context_block}\n\n" if context_block else ''
    ) + f"Attributes and levels:\n{attr_block}\n\nLanguage: {language}"

    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])
        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write system prompts for a large language model that generates textual stimuli '
                        'for conjoint choice experiments. '
                        'Given a set of attributes and levels, produce a prompt that instructs the LLM to write '
                        'a short, realistic description (3–5 sentences) that embeds the attribute values naturally '
                        'in a coherent narrative — not as a list. '
                        'The prompt must end with a sentence like '
                        '"The specific attribute values for this option are:" so that the profile can be appended. '
                        'Match the language instruction. '
                        'Output only the prompt text — no explanation, no markdown fences.'
                    ),
                },
                {'role': 'user', 'content': user_msg},
            ],
            temperature=0.6,
            max_tokens=500,
        )
        return jsonify(prompt=resp.choices[0].message.content.strip())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route('/api/optimize-image-prompt', methods=['POST'])
@login_required
def api_optimize_image_prompt():
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured.'), 503
    data = request.get_json(force=True) or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify(error='Prompt is empty.'), 400
    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])
        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are an expert at writing AI image generation prompts for academic research. '
                        'The prompt will be extended with specific attribute-level profile details for each image. '
                        'Transform the given description into an effective base prompt: concrete, '
                        'visually specific, free of abstract concepts. '
                        'Output only the optimized prompt — no explanation, no preamble.'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.5,
            max_tokens=300,
        )
        return jsonify(prompt=resp.choices[0].message.content.strip())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route('/api/image-prompt-feedback', methods=['POST'])
@login_required
def api_image_prompt_feedback():
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured.'), 503
    data = request.get_json(force=True) or {}
    base_prompt = (data.get('prompt') or '').strip()
    profile_parts = data.get('profile_parts') or []
    if not base_prompt:
        return jsonify(error='Prompt is empty.'), 400

    full_prompt = base_prompt
    if profile_parts:
        full_prompt += '. ' + ', '.join(profile_parts) + '.'
    full_prompt = full_prompt[:1000]

    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])
        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You evaluate AI image generation prompts for conjoint survey research. '
                        'Be concise and practical. Structure your feedback as: '
                        '(1) Assessment, (2) Potential issues, (3) Specific improvements.'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        f'Full prompt that would be sent to DALL-E for one profile '
                        f'({len(full_prompt)}/1000 chars):\n\n"{full_prompt}"\n\n'
                        'Will this produce consistent, controlled stimuli suitable for a conjoint study? '
                        'What should be improved?'
                    ),
                },
            ],
            temperature=0.4,
            max_tokens=500,
        )
        return jsonify(feedback=resp.choices[0].message.content.strip())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route('/surveys/<int:survey_id>/delete', methods=['POST'])
@login_required
def surveys_delete(survey_id):
    survey = Survey.query.get_or_404(survey_id)
    # Allow delete for owners regardless of active/archived status
    membership = SurveyUser.query.filter_by(
        survey_id=survey_id, user_id=g.current_user.id, role='owner'
    ).first()
    if not membership:
        abort(403)
    title = survey.title
    db.session.delete(survey)
    db.session.commit()
    flash(f'Survey "{title}" permanently deleted.', 'danger')
    return redirect(url_for('surveys_list'))


@app.route('/surveys/<int:survey_id>/update-redirect', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_update_redirect(survey_id):
    g.survey.redirect_url = request.form.get('redirect_url', '').strip() or None
    db.session.commit()
    flash('Redirect URL updated.', 'success')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/archive', methods=['POST'])
@survey_access_required()
def surveys_archive(survey_id):
    g.membership.status = 'archived'
    db.session.commit()
    flash('Survey archived.', 'info')
    return redirect(url_for('surveys_list'))


@app.route('/surveys/<int:survey_id>/leave', methods=['POST'])
@survey_access_required()
def surveys_leave(survey_id):
    if g.membership.role == 'owner':
        flash('Owners cannot leave their own survey. Archive it instead.', 'warning')
        return redirect(url_for('surveys_detail', survey_id=survey_id))
    g.membership.status = 'left'
    db.session.commit()
    flash('You have left the survey.', 'info')
    return redirect(url_for('surveys_list'))


@app.route('/surveys/<int:survey_id>/restore', methods=['POST'])
@login_required
def surveys_restore(survey_id):
    membership = SurveyUser.query.filter_by(
        survey_id=survey_id, user_id=g.current_user.id
    ).first_or_404()
    membership.status = 'active'
    db.session.commit()
    flash('Survey restored.', 'success')
    return redirect(url_for('surveys_list'))


# ---------------------------------------------------------------------------
# Sharing / collaboration
# ---------------------------------------------------------------------------

@app.route('/surveys/<int:survey_id>/invite', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_invite(survey_id):
    survey = g.survey
    orcid = request.form.get('orcid', '').strip()
    if not orcid:
        flash('Please enter an ORCID.', 'warning')
        return redirect(url_for('surveys_detail', survey_id=survey_id))

    collaborator = User.query.filter_by(orcid=orcid).first()
    if collaborator:
        if collaborator.id == g.current_user.id:
            flash('You cannot add yourself.', 'warning')
        else:
            existing = SurveyUser.query.filter_by(
                survey_id=survey_id, user_id=collaborator.id
            ).first()
            if existing and existing.status == 'active':
                flash(f'{collaborator.name or orcid} is already a collaborator.', 'info')
            else:
                if existing:
                    existing.status = 'active'
                    existing.role = 'collaborator'
                else:
                    db.session.add(SurveyUser(
                        survey_id=survey_id, user_id=collaborator.id,
                        role='collaborator', status='active',
                    ))
                db.session.commit()
                flash(f'Added {collaborator.name or orcid} as collaborator.', 'success')
    else:
        existing_invite = PendingInvite.query.filter_by(
            survey_id=survey_id, orcid=orcid
        ).first()
        if existing_invite:
            flash(f'ORCID {orcid} has already been invited and will be added when they first log in.', 'info')
        else:
            db.session.add(PendingInvite(survey_id=survey_id, orcid=orcid))
            db.session.commit()
            flash(f'Invited ORCID {orcid}. They will be added as a collaborator when they first log in.', 'success')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/share', methods=['GET', 'POST'])
@survey_access_required(role_needed='owner')
def surveys_share(survey_id):
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/collaborators/<int:collab_id>/remove', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_remove_collaborator(survey_id, collab_id):
    membership = SurveyUser.query.filter_by(
        survey_id=survey_id, user_id=collab_id, role='collaborator'
    ).first_or_404()
    membership.status = 'left'
    db.session.commit()
    flash('Collaborator removed.', 'info')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


@app.route('/surveys/<int:survey_id>/invites/<int:invite_id>/remove', methods=['POST'])
@survey_access_required(role_needed='owner')
def surveys_remove_invite(survey_id, invite_id):
    invite = PendingInvite.query.filter_by(id=invite_id, survey_id=survey_id).first_or_404()
    db.session.delete(invite)
    db.session.commit()
    flash('Pending invite removed.', 'info')
    return redirect(url_for('surveys_detail', survey_id=survey_id))


# ---------------------------------------------------------------------------
# Design details / YAML export
# ---------------------------------------------------------------------------

def _build_design_text(survey, attributes):
    """Plain-text tree of the full survey design, suitable for copying."""
    lines = [
        f"Survey:       {survey.title}",
        f"Language:     {survey.language or 'English'}",
        f"Presentation: {survey.presentation_type}",
        f"Tasks:        {survey.num_tasks}   Alternatives per task: {survey.num_alternatives}" +
        (f"   D-efficiency: {survey.d_efficiency:.4f}" if survey.d_efficiency else ""),
    ]
    if survey.description:
        lines += ["", "Description:", survey.description]
    if survey.intro_text:
        lines += ["", "Participant introduction:", survey.intro_text]
    lines += ["", "=" * 60, "", "Attributes & Levels", ""]
    for i, attr in enumerate(attributes, 1):
        lines.append(f"{i}. {attr.name}")
        if attr.description:
            lines.append(f"   Participant description: {attr.description}")
        lines.append("")
        for j, lv in enumerate(attr.levels, 1):
            lines.append(f"   {i}.{j}  {lv.name}")
            if lv.description:
                lines.append(f"        Participant description: {lv.description}")
            if lv.llm_hint:
                lines.append(f"        LLM hint:                {lv.llm_hint}")
        lines.append("")
    return "\n".join(lines)


def _build_design_yaml(survey, attributes):
    import yaml as _yaml
    attr_col_names = [f"attr_{a.order_index + 1}_{_slugify(a.name)}" for a in attributes]

    response_columns = [
        {'column': 'participant_key',  'description': 'Participant identifier from the external survey tool'},
        {'column': 'block',            'description': 'Block number (1-indexed); 1 for all participants in non-blocked designs'},
        {'column': 'started_at',       'description': 'Timestamp when the participant entered the conjoint'},
        {'column': 'completed_at',     'description': 'Timestamp when the participant finished (empty if incomplete)'},
        {'column': 'task',             'description': 'Sequential task number within the participant\'s block (1-indexed)'},
        {'column': 'alternative',      'description': 'Alternative number within the task (1-indexed)'},
        {'column': 'chosen',           'description': '1 if this alternative was selected, 0 otherwise'},
        {'column': 'response_time_ms', 'description': 'Response time in milliseconds'},
    ] + [
        {'column': col, 'description': f'Level name for attribute: {a.name}'}
        for col, a in zip(attr_col_names, attributes)
    ]

    data = {
        'title':             survey.title,
        'description':       survey.description or '',
        'language':          survey.language or 'English',
        'presentation_type': survey.presentation_type,
        'num_tasks':         survey.num_tasks,
        'num_alternatives':  survey.num_alternatives,
        'created_at':        survey.created_at.strftime('%Y-%m-%d %H:%M UTC') if survey.created_at else '',
        'redirect_url':      survey.redirect_url or '',
        'intro_text':        survey.intro_text or '',
        'attributes':        [],
        'data_dictionary': {
            'responses_csv':     response_columns,
            'design_matrix_csv': (
                'One row per profile (task × alternative). '
                'For blocked designs includes a block column and covers all blocks. '
                'Same attribute columns as responses.csv. '
                'Use to verify the experimental design independently of the response data.'
            ),
        },
    }
    if survey.d_efficiency:
        data['d_efficiency'] = round(survey.d_efficiency, 6)
    if survey.presentation_type == 'textual':
        data['data_dictionary']['generated_texts_csv'] = (
            'One row per profile. Columns: task, alternative, '
            'attribute columns (same as above), generated_text '
            '— the AI-generated stimulus text shown to participants.'
        )

    for attr in attributes:
        a_entry = {
            'name':        attr.name,
            'description': attr.description or '',
            'levels':      [],
        }
        for lv in attr.levels:
            lv_entry = {'name': lv.name, 'description': lv.description or ''}
            if lv.llm_hint:
                lv_entry['llm_hint'] = lv.llm_hint
            a_entry['levels'].append(lv_entry)
        data['attributes'].append(a_entry)

    return _yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


@app.route('/surveys/<int:survey_id>/design-details')
@survey_access_required()
def surveys_design_details(survey_id):
    survey = g.survey
    attributes = Attribute.query.filter_by(
        survey_id=survey_id
    ).order_by(Attribute.order_index).all()
    plain_text = _build_design_text(survey, attributes)
    return render_template('surveys/design_details.html',
                           survey=survey,
                           attributes=attributes,
                           plain_text=plain_text)


@app.route('/surveys/<int:survey_id>/design.yaml')
@survey_access_required()
def surveys_design_yaml(survey_id):
    survey = g.survey
    attributes = Attribute.query.filter_by(
        survey_id=survey_id
    ).order_by(Attribute.order_index).all()
    yaml_str = _build_design_yaml(survey, attributes)
    filename = f"{_slugify(survey.title)}_design.yaml"
    resp = make_response(yaml_str)
    resp.headers['Content-Type'] = 'application/x-yaml; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


# Data export (ZIP)
# ---------------------------------------------------------------------------

@app.route('/surveys/<int:survey_id>/export')
@survey_access_required()
def surveys_export(survey_id):
    survey = g.survey
    attributes = Attribute.query.filter_by(
        survey_id=survey_id
    ).order_by(Attribute.order_index).all()

    attr_map = {a.id: a for a in attributes}
    level_map = {}
    for a in attributes:
        for lv in a.levels:
            level_map[lv.id] = lv

    participants = (
        Participant.query.filter_by(survey_id=survey_id)
        .order_by(Participant.started_at)
        .all()
    )

    # ── responses.csv ────────────────────────────────────────────────────────
    csv_buf = io.StringIO()
    attr_col_names = [f"attr_{a.order_index + 1}_{_slugify(a.name)}" for a in attributes]
    is_blocked = survey.pool_size and survey.pool_size > survey.num_tasks
    fieldnames = [
        'participant_key', 'block', 'started_at', 'completed_at',
        'task', 'alternative', 'chosen', 'response_time_ms',
    ] + attr_col_names

    writer = csv.DictWriter(csv_buf, fieldnames=fieldnames)
    writer.writeheader()

    for p in participants:
        # Determine block number (1-indexed) from the participant's task assignment
        if is_blocked and p.task_assignment:
            assignment = json.loads(p.task_assignment)
            block_num = assignment[0] // survey.num_tasks + 1
        else:
            block_num = 1

        resp_by_task = {r.task_index: r for r in p.responses}
        for task_idx in range(survey.num_tasks):
            resp = resp_by_task.get(task_idx)
            # Resolve sequential position to the actual global design task index
            design_task_idx = _resolve_design_task(p, task_idx)
            design_cells = (
                DesignTask.query
                .filter_by(survey_id=survey_id, task_index=design_task_idx)
                .order_by(DesignTask.alternative_index)
                .all()
            )
            for alt_idx, cell in enumerate(design_cells):
                profile = json.loads(cell.profile)
                row = {
                    'participant_key': p.external_key,
                    'block': block_num,
                    'started_at': p.started_at.strftime('%Y-%m-%dT%H:%M:%S'),
                    'completed_at': p.completed_at.strftime('%Y-%m-%dT%H:%M:%S') if p.completed_at else '',
                    'task': task_idx + 1,
                    'alternative': alt_idx + 1,
                    'chosen': 1 if (resp and resp.chosen_alternative == alt_idx) else 0,
                    'response_time_ms': resp.response_time_ms if resp else '',
                }
                for a in attributes:
                    lv_id = profile.get(str(a.id))
                    lv = level_map.get(lv_id)
                    row[f"attr_{a.order_index + 1}_{_slugify(a.name)}"] = lv.name if lv else ''
                writer.writerow(row)

    # ── design_matrix.csv ────────────────────────────────────────────────────
    dm_buf = io.StringIO()
    pool_size_dm = survey.pool_size if is_blocked else survey.num_tasks
    dm_fields = (['block', 'task'] if is_blocked else ['task']) + ['alternative'] + attr_col_names
    dm_writer = csv.DictWriter(dm_buf, fieldnames=dm_fields)
    dm_writer.writeheader()

    for task_idx in range(pool_size_dm):
        cells = (
            DesignTask.query
            .filter_by(survey_id=survey_id, task_index=task_idx)
            .order_by(DesignTask.alternative_index)
            .all()
        )
        for alt_idx, cell in enumerate(cells):
            profile = json.loads(cell.profile)
            row = {'alternative': alt_idx + 1}
            if is_blocked:
                row['block'] = task_idx // survey.num_tasks + 1
                row['task'] = task_idx % survey.num_tasks + 1
            else:
                row['task'] = task_idx + 1
            for a in attributes:
                lv_id = profile.get(str(a.id))
                lv = level_map.get(lv_id)
                row[f"attr_{a.order_index + 1}_{_slugify(a.name)}"] = lv.name if lv else ''
            dm_writer.writerow(row)

    # ── generated_texts.csv (textual surveys only) ───────────────────────────
    gt_buf = None
    if survey.presentation_type == 'textual' and survey.texts_generated:
        pool_size_val = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks
        gt_pk_map = {gt.profile_key: gt for gt in GeneratedText.query.filter_by(survey_id=survey_id)}
        dt_index = {
            (c.task_index, c.alternative_index): json.loads(c.profile)
            for c in DesignTask.query.filter_by(survey_id=survey_id)
        }
        gt_buf = io.StringIO()
        gt_fields = ['task', 'alternative'] + attr_col_names + ['generated_text', 'prompt']
        gt_writer = csv.DictWriter(gt_buf, fieldnames=gt_fields)
        gt_writer.writeheader()
        for t in range(pool_size_val):
            for a_idx in range(survey.num_alternatives):
                profile = dt_index.get((t, a_idx), {})
                if not profile:
                    continue
                pk = _profile_key(profile)
                gt = gt_pk_map.get(pk)
                if not gt:
                    continue
                row = {'task': t + 1, 'alternative': a_idx + 1}
                for a in attributes:
                    lv_id = profile.get(str(a.id))
                    lv = level_map.get(lv_id)
                    row[f"attr_{a.order_index + 1}_{_slugify(a.name)}"] = lv.name if lv else ''
                row['generated_text'] = gt.text
                row['prompt'] = gt.prompt or ''
                gt_writer.writerow(row)

    # ── r_starter.R ──────────────────────────────────────────────────────────
    r_script = _build_r_starter(survey, attributes)

    # ── images/ folder + image_prompts.csv (visual surveys only) ────────────
    image_exports = []
    img_prompts_buf = None
    if survey.presentation_type == 'visual' and survey.images_generated:
        pool_size_val = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks
        gi_pk_map = {gi.profile_key: gi for gi in GeneratedImage.query.filter_by(survey_id=survey_id)}
        img_dt_index = {
            (c.task_index, c.alternative_index): json.loads(c.profile)
            for c in DesignTask.query.filter_by(survey_id=survey_id)
        }
        img_prompts_buf = io.StringIO()
        ip_fields = ['task', 'alternative'] + attr_col_names + ['prompt']
        ip_writer = csv.DictWriter(img_prompts_buf, fieldnames=ip_fields)
        ip_writer.writeheader()
        for t in range(pool_size_val):
            for a_idx in range(survey.num_alternatives):
                profile = img_dt_index.get((t, a_idx), {})
                if not profile:
                    continue
                pk = _profile_key(profile)
                gi = gi_pk_map.get(pk)
                if not gi:
                    continue
                slug_parts = []
                ip_row = {'task': t + 1, 'alternative': a_idx + 1}
                for a in attributes:
                    lv_id = profile.get(str(a.id))
                    lv = level_map.get(lv_id)
                    col = f"attr_{a.order_index + 1}_{_slugify(a.name)}"
                    ip_row[col] = lv.name if lv else ''
                    if lv:
                        slug_parts.append(_slugify(lv.name))
                ip_row['prompt'] = gi.prompt or ''
                ip_writer.writerow(ip_row)
                slug = '_'.join(slug_parts[:3])
                fname = f"images/task{t+1:02d}_alt{a_idx+1:02d}_{slug}.png"
                image_exports.append((fname, gi.image_data))

    # ── ZIP ──────────────────────────────────────────────────────────────────
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('design.yaml', _build_design_yaml(survey, attributes))
        zf.writestr('responses.csv', csv_buf.getvalue())
        zf.writestr('design_matrix.csv', dm_buf.getvalue())
        if gt_buf is not None:
            zf.writestr('generated_texts.csv', gt_buf.getvalue())
        if img_prompts_buf is not None:
            zf.writestr('image_prompts.csv', img_prompts_buf.getvalue())
        for fname, data in image_exports:
            zf.writestr(fname, data)
        zf.writestr('r_starter.R', r_script)
    zip_buf.seek(0)

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M')
    filename = f"conjoint_{_slugify(survey.title)}_{timestamp}.zip"

    resp = make_response(zip_buf.read())
    resp.headers['Content-Type'] = 'application/zip'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


def _slugify(s: str) -> str:
    import re
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')[:30]


def _build_r_starter(survey, attributes) -> str:
    attr_cols = [f"attr_{a.order_index + 1}_{_slugify(a.name)}" for a in attributes]
    attr_cols_r = ', '.join(f'"{c}"' for c in attr_cols)
    lines = [
        '# R starter script for conjoint analysis',
        f'# Survey: {survey.title}',
        f'# Generated: {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC',
        '',
        'library(tidyverse)',
        '',
        '# Load data',
        'df <- read_csv("responses.csv")',
        '',
        '# Inspect',
        'glimpse(df)',
        '',
        f'# Attribute columns: {attr_cols_r}',
        'attr_cols <- c(' + attr_cols_r + ')',
        '',
        '# Convert attribute columns to factors (preserving level order from design)',
        'df <- df |>',
        '  mutate(across(all_of(attr_cols), as.factor))',
        '',
        '# Basic descriptives',
        'df |> count(chosen)',
        '',
        '# Example: conditional logit using survival package',
        '# install.packages("survival")',
        '# library(survival)',
        '# model <- clogit(',
        '#   chosen ~ ' + ' + '.join(attr_cols) + ' + strata(participant_key, task),',
        '#   data = df',
        '# )',
        '# summary(model)',
        '',
        '# Example: mixed logit using mlogit',
        '# install.packages("mlogit")',
        '# library(mlogit)',
        '# df_mlogit <- mlogit.data(',
        '#   df, choice = "chosen", shape = "long",',
        '#   alt.var = "alternative", id.var = "participant_key"',
        '# )',
        '# model_ml <- mlogit(chosen ~ ' + ' + '.join(attr_cols) + ' | 0, df_mlogit)',
        '# summary(model_ml)',
    ]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# API: AI design suggestion
# ---------------------------------------------------------------------------

@app.route('/api/generate-attr-details', methods=['POST'])
@login_required
def api_generate_attr_details():
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured.'), 503

    data = request.get_json(force=True) or {}
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    language = (data.get('language') or 'English').strip()
    presentation_type = (data.get('presentation_type') or 'tabular').strip()
    attr_name = (data.get('attribute_name') or '').strip()
    levels = [str(l).strip() for l in (data.get('levels') or []) if str(l).strip()]

    if not attr_name or not levels:
        return jsonify(error='attribute_name and levels are required.'), 400

    pres_note = {
        'textual': 'Options are presented as AI-generated text descriptions.',
        'visual': 'Options are presented as AI-generated images (DALL-E).',
        'tabular': 'Options are presented in a table (attribute rows, option columns).',
    }.get(presentation_type, '')

    level_list = '\n'.join(f'- {l}' for l in levels)
    context = f'Survey title: "{title}"\n'
    if description:
        context += f'Survey context: {description}\n'
    context += f'Language: {language}\n'
    if pres_note:
        context += f'Presentation: {pres_note}\n'

    prompt = f"""{context}
Attribute: "{attr_name}"
Levels:
{level_list}

For this conjoint survey attribute, generate the following. Return valid JSON only.

"attribute_description": One short sentence (plain language, no jargon) shown to participants explaining what this dimension means.

For each level in the same order, generate:
- "description": Brief participant-facing clarification (one phrase or short sentence). Leave empty if the level name is self-explanatory.
- "llm_hint": Specific, concrete guidance for AI text/image generation — visual, physical, or stylistic qualities that distinguish this level. Focus on what makes it distinct and perceptible. Leave empty if nothing meaningful can be added.

JSON:
{{
  "attribute_description": "...",
  "levels": [
    {{"name": "<exact level name>", "description": "...", "llm_hint": "..."}},
    ...
  ]
}}"""

    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])
        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[{'role': 'user', 'content': prompt}],
            response_format={'type': 'json_object'},
            max_tokens=600,
            temperature=0.6,
        )
        result = json.loads(resp.choices[0].message.content)
        return jsonify(result)
    except Exception as exc:
        app.logger.error('generate-attr-details error: %s', exc)
        return jsonify(error=str(exc)), 500


@app.route('/api/suggest-design', methods=['POST'])
@login_required
def api_suggest_design():
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured. Add OPENAI_API_KEY to your .env file.'), 503

    data = request.get_json(force=True) or {}
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    num_attributes = max(1, min(6, int(data.get('num_attributes') or 4)))
    num_levels = max(2, min(4, int(data.get('num_levels') or 2)))

    if not title:
        return jsonify(error='Please fill in the survey title on page 1 first.'), 400

    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])

        level_example = ', '.join([f'{{"name":"...","description":"..."}}'] * num_levels)
        system_msg = (
            "You are an expert in conjoint analysis for social-science survey research. "
            "You always respond with valid JSON and nothing else."
        )
        user_msg = (
            f"Design a conjoint experiment for the following study:\n"
            f"Title: {title}\n"
            f"Description: {description or '(none provided)'}\n\n"
            "Requirements:\n"
            f"- Exactly {num_attributes} attributes, each with exactly {num_levels} levels.\n"
            "- Attribute names: concise table headers, max 4 words.\n"
            "- Attribute descriptions: one plain sentence a survey participant can read and understand.\n"
            "- Level names: short concrete values (1–3 words), directly comparable in a table.\n"
            "- Level descriptions: brief optional clarification (can be an empty string).\n"
            "- Respond in the same language as the survey title.\n\n"
            "Return JSON matching this exact structure (no additional keys):\n"
            f'{{"attributes":[{{"name":"...","description":"...","levels":[{level_example}]}}]}}'
        )

        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            temperature=0.7,
            max_tokens=max(900, num_attributes * num_levels * 120),
        )

        result = json.loads(resp.choices[0].message.content)
        if 'attributes' not in result or not isinstance(result['attributes'], list):
            raise ValueError('Unexpected response structure from OpenAI')

        return jsonify(result)

    except Exception as exc:
        app.logger.error('OpenAI suggest-design error: %s', exc)
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: generate participant introduction text
# ---------------------------------------------------------------------------

@app.route('/api/generate-intro', methods=['POST'])
@login_required
def api_generate_intro():
    if not app.config.get('OPENAI_API_KEY'):
        return jsonify(error='OpenAI API key not configured. Add OPENAI_API_KEY to your .env file.'), 503

    data = request.get_json(force=True) or {}
    prompt = (data.get('prompt') or '').strip()

    if not prompt:
        return jsonify(error='Prompt is empty.'), 400

    try:
        from openai import OpenAI as _OpenAI
        ai = _OpenAI(api_key=app.config['OPENAI_API_KEY'])

        resp = ai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write clear, accessible introductory text for survey participants. '
                        'Output only the requested text — no headings, no markdown, '
                        'no meta-commentary, no preamble.'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.7,
            max_tokens=600,
        )

        text = resp.choices[0].message.content.strip()
        return jsonify(text=text)

    except Exception as exc:
        app.logger.error('generate-intro error: %s', exc)
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: sample size estimate
# ---------------------------------------------------------------------------

@app.route('/api/sample-size')
def api_sample_size():
    try:
        num_tasks = int(request.args.get('tasks', 8))
        num_alts = int(request.args.get('alts', 2))
        max_levels = int(request.args.get('max_levels', 3))
    except (ValueError, TypeError):
        return jsonify(error='Invalid parameters'), 400

    minimum, recommended = estimate_sample_size(num_tasks, num_alts, max_levels)
    return jsonify(minimum=minimum, recommended=recommended)


# ---------------------------------------------------------------------------
# Participant flow
# ---------------------------------------------------------------------------

@app.route('/s/<string:public_id>')
def participant_entry(public_id):
    survey = Survey.query.filter_by(public_id=public_id).first_or_404()
    if not survey.design_generated:
        return render_template('participant/unavailable.html', survey=survey)
    if survey.presentation_type == 'textual' and not survey.texts_generated:
        return render_template('participant/unavailable.html', survey=survey)
    if survey.presentation_type == 'visual' and not survey.images_generated:
        return render_template('participant/unavailable.html', survey=survey)

    external_key = request.args.get('key', '').strip()
    if not external_key:
        # No key in URL: use (or create) a session-scoped anonymous key so the
        # participant can resume if they revisit the same URL in the same browser.
        _anon_session_key = f'anon_key_{survey.id}'
        if _anon_session_key not in session:
            session[_anon_session_key] = 'anon_' + secrets.token_hex(8)
        external_key = session[_anon_session_key]

    participant = Participant.query.filter_by(
        survey_id=survey.id, external_key=external_key
    ).first()

    if participant and participant.completed_at:
        return redirect(_build_redirect_url(survey, external_key))

    prolific_pid = request.args.get('PROLIFIC_PID', '').strip() or None
    study_id     = request.args.get('STUDY_ID',     '').strip() or None
    session_id   = request.args.get('SESSION_ID',   '').strip() or None

    if not participant:
        participant = Participant(
            survey_id=survey.id, external_key=external_key, current_task=0,
            prolific_pid=prolific_pid, study_id=study_id, session_id=session_id,
        )
        db.session.add(participant)
        db.session.flush()
    else:
        # Update on re-entry in case params were missing on first visit
        if prolific_pid and not participant.prolific_pid:
            participant.prolific_pid = prolific_pid
        if study_id and not participant.study_id:
            participant.study_id = study_id
        if session_id and not participant.session_id:
            participant.session_id = session_id

    # Assign a random block for blocked designs (Sawtooth-style)
    if survey.pool_size and survey.pool_size > survey.num_tasks and not participant.task_assignment:
        import random as _random
        num_blocks = survey.pool_size // survey.num_tasks
        block_idx = _random.randint(0, num_blocks - 1)
        block_tasks = list(range(block_idx * survey.num_tasks, (block_idx + 1) * survey.num_tasks))
        _random.shuffle(block_tasks)  # randomize presentation order within block
        participant.task_assignment = json.dumps(block_tasks)

    db.session.commit()
    session[f'participant_{survey.id}'] = participant.id
    # Show intro only for new participants (haven't started yet)
    if survey.show_intro and survey.intro_text and participant.current_task == 0:
        return redirect(url_for('participant_intro', public_id=public_id))
    return redirect(url_for('participant_task', public_id=public_id,
                            task_index=participant.current_task))


_CONSENT_TEXT = {
    'english':    'I have read and understood the information above and agree to participate voluntarily.',
    'german':     'Ich habe die obigen Informationen gelesen und verstanden und erkläre mich zur freiwilligen Teilnahme bereit.',
    'deutsch':    'Ich habe die obigen Informationen gelesen und verstanden und erkläre mich zur freiwilligen Teilnahme bereit.',
    'french':     "J'ai lu et compris les informations ci-dessus et j'accepte de participer volontairement.",
    'français':   "J'ai lu et compris les informations ci-dessus et j'accepte de participer volontairement.",
    'spanish':    'He leído y comprendido la información anterior y acepto participar voluntariamente.',
    'español':    'He leído y comprendido la información anterior y acepto participar voluntariamente.',
    'dutch':      'Ik heb de bovenstaande informatie gelezen en begrepen en ga akkoord met vrijwillige deelname.',
    'nederlands': 'Ik heb de bovenstaande informatie gelezen en begrepen en ga akkoord met vrijwillige deelname.',
    'italian':    'Ho letto e compreso le informazioni di cui sopra e accetto di partecipare volontariamente.',
    'italiano':   'Ho letto e compreso le informazioni di cui sopra e accetto di partecipare volontariamente.',
    'portuguese': 'Li e compreendi as informações acima e concordo em participar voluntariamente.',
    'português':  'Li e compreendi as informações acima e concordo em participar voluntariamente.',
    'chinese':    '我已阅读并理解上述信息，同意自愿参与。',
    'japanese':   '上記の情報を読んで理解し、自発的に参加することに同意します。',
    'polish':     'Przeczytałem/am i zrozumiałem/am powyższe informacje i zgadzam się na dobrowolny udział.',
    'polski':     'Przeczytałem/am i zrozumiałem/am powyższe informacje i zgadzam się na dobrowolny udział.',
    'swedish':    'Jag har läst och förstått ovanstående information och samtycker till att delta frivilligt.',
    'svenska':    'Jag har läst och förstått ovanstående information och samtycker till att delta frivilligt.',
}


@app.route('/s/<string:public_id>/intro')
def participant_intro(public_id):
    survey = Survey.query.filter_by(public_id=public_id).first_or_404()
    participant_id = session.get(f'participant_{survey.id}')
    if not participant_id:
        return redirect(url_for('participant_entry', public_id=public_id))
    lang_key = (survey.language or 'English').strip().lower()
    consent_text = _CONSENT_TEXT.get(lang_key, _CONSENT_TEXT['english'])
    return render_template('participant/intro.html', survey=survey, consent_text=consent_text)


@app.route('/s/<string:public_id>/task/<int:task_index>', methods=['GET', 'POST'])
def participant_task(public_id, task_index):
    survey = Survey.query.filter_by(public_id=public_id).first_or_404()
    survey_id = survey.id

    participant_id = session.get(f'participant_{survey_id}')
    if not participant_id:
        return redirect(url_for('participant_entry', public_id=public_id))

    participant = Participant.query.get_or_404(participant_id)

    if task_index >= survey.num_tasks:
        return redirect(url_for('participant_complete', public_id=public_id))

    # Don't allow skipping ahead
    if task_index > participant.current_task:
        return redirect(url_for('participant_task', public_id=public_id,
                                task_index=participant.current_task))

    if request.method == 'POST':
        raw_choice = request.form.get('choice')
        if raw_choice is None:
            flash('Please select one of the options.', 'warning')
            return redirect(url_for('participant_task', public_id=public_id,
                                    task_index=task_index))

        chosen_alt = int(raw_choice)  # 0-indexed
        design_task_idx = _resolve_design_task(participant, task_index)

        # Response time: client sends task load timestamp (ms since epoch)
        try:
            load_ms = int(request.form.get('task_load_ms', 0))
            now_ms = int(datetime.utcnow().timestamp() * 1000)
            rt_ms = now_ms - load_ms if load_ms else None
        except (ValueError, TypeError):
            rt_ms = None

        cells = (
            DesignTask.query
            .filter_by(survey_id=survey_id, task_index=design_task_idx)
            .order_by(DesignTask.alternative_index)
            .all()
        )
        choice_set_snapshot = [json.loads(c.profile) for c in cells]

        existing = Response.query.filter_by(
            participant_id=participant_id, task_index=task_index
        ).first()
        if existing:
            existing.chosen_alternative = chosen_alt
            existing.responded_at = datetime.utcnow()
            existing.response_time_ms = rt_ms
            existing.choice_set = json.dumps(choice_set_snapshot)
        else:
            db.session.add(Response(
                participant_id=participant_id,
                task_index=task_index,
                chosen_alternative=chosen_alt,
                response_time_ms=rt_ms,
                choice_set=json.dumps(choice_set_snapshot),
            ))

        next_task = task_index + 1
        participant.current_task = max(participant.current_task, next_task)

        if next_task >= survey.num_tasks:
            participant.completed_at = datetime.utcnow()
            db.session.commit()
            return redirect(url_for('participant_complete', public_id=public_id))

        db.session.commit()
        return redirect(url_for('participant_task', public_id=public_id,
                                task_index=next_task))

    # GET — build the task display
    # Resolve participant's sequential task index to design task index (blocked designs)
    design_task_idx = _resolve_design_task(participant, task_index)

    cells = (
        DesignTask.query
        .filter_by(survey_id=survey_id, task_index=design_task_idx)
        .order_by(DesignTask.alternative_index)
        .all()
    )
    attributes = (
        Attribute.query.filter_by(survey_id=survey_id)
        .order_by(Attribute.order_index)
        .all()
    )
    mode = survey.attr_order_mode or ('task' if survey.randomise_attrs else 'none')
    if survey.presentation_type == 'tabular' and mode != 'none':
        import random as _rand
        attributes = list(attributes)
        if mode == 'task':
            _rand.shuffle(attributes)
        elif mode == 'participant':
            sess_key = f'attr_order_{survey_id}'
            if sess_key not in session:
                order = [a.id for a in attributes]
                _rand.shuffle(order)
                session[sess_key] = order
            id_to_attr = {a.id: a for a in attributes}
            attributes = [id_to_attr[aid] for aid in session[sess_key] if aid in id_to_attr]
    level_map = {}
    for a in attributes:
        for lv in a.levels:
            level_map[lv.id] = lv

    alternatives = []
    for cell in cells:
        profile = json.loads(cell.profile)
        alt_levels = {a.id: level_map[profile[str(a.id)]] for a in attributes}
        alternatives.append(alt_levels)

    # For textual presentation, look up texts by profile_key for each cell
    generated_texts = {}
    if survey.presentation_type == 'textual':
        gt_map = {gt.profile_key: gt.text
                  for gt in GeneratedText.query.filter_by(survey_id=survey_id)}
        for cell in cells:
            pk = _profile_key(json.loads(cell.profile))
            if pk in gt_map:
                generated_texts[cell.alternative_index] = gt_map[pk]

    return render_template(
        'participant/task.html',
        survey=survey,
        task_index=task_index,
        design_task_idx=design_task_idx,
        total_tasks=survey.num_tasks,
        attributes=attributes,
        alternatives=alternatives,
        generated_texts=generated_texts,
        progress=int(100 * task_index / survey.num_tasks),
    )


@app.route('/s/<string:public_id>/complete')
def participant_complete(public_id):
    survey = Survey.query.filter_by(public_id=public_id).first_or_404()
    participant_id = session.get(f'participant_{survey.id}')
    participant = Participant.query.get(participant_id) if participant_id else None
    external_key = participant.external_key if participant else ''
    redirect_url = _build_redirect_url(survey, external_key) if survey.redirect_url else None
    return render_template('participant/complete.html',
                           survey=survey, redirect_url=redirect_url,
                           external_key=external_key)


def _build_redirect_url(survey, external_key: str) -> str:
    if not survey.redirect_url:
        return ''
    return survey.redirect_url.replace('%KEY%', external_key)


# ---------------------------------------------------------------------------
# Design generation helpers
# ---------------------------------------------------------------------------

def _resolve_design_task(participant, task_index: int) -> int:
    """Map a participant's sequential task index to the actual design task index."""
    if participant.task_assignment:
        assignment = json.loads(participant.task_assignment)
        if task_index < len(assignment):
            return assignment[task_index]
    return task_index


def _profile_key(profile: dict) -> str:
    """Canonical JSON string for a profile dict {attr_id: level_id}."""
    return json.dumps({k: v for k, v in sorted(profile.items(), key=lambda x: int(x[0]))},
                      separators=(',', ':'))


def _generate_and_store_design(survey: Survey) -> float:
    """(Re-)generate the conjoint design and write it to the DB. Returns D-efficiency."""
    attributes = (
        Attribute.query.filter_by(survey_id=survey.id)
        .order_by(Attribute.order_index)
        .all()
    )
    attributes_levels = [len(a.levels) for a in attributes]

    # Blocked design uses a larger pool; participants see a random num_tasks subset
    design_tasks = survey.pool_size if (survey.pool_size and survey.pool_size > survey.num_tasks) else survey.num_tasks

    design, d_eff = generate_design(
        attributes_levels,
        design_tasks,
        survey.num_alternatives,
        n_starts=20,
    )

    # Clear old design
    DesignTask.query.filter_by(survey_id=survey.id).delete()

    dupe_task_count = 0
    for task_idx in range(design_tasks):
        task_profiles = []
        task_has_dupe = False
        for alt_idx in range(survey.num_alternatives):
            profile = {}
            for attr_idx, attr in enumerate(attributes):
                level_idx = int(design[task_idx, alt_idx, attr_idx])
                level = attr.levels[level_idx]
                profile[str(attr.id)] = level.id
            profile_tuple = tuple(sorted(profile.items()))
            if profile_tuple in task_profiles:
                task_has_dupe = True
            task_profiles.append(profile_tuple)
            db.session.add(DesignTask(
                survey_id=survey.id,
                task_index=task_idx,
                alternative_index=alt_idx,
                profile=json.dumps(profile),
            ))
        if task_has_dupe:
            dupe_task_count += 1

    survey.has_duplicate_profiles = dupe_task_count > 0
    survey.duplicate_profile_count = dupe_task_count
    survey.design_generated = True
    survey.design_stale = False
    return d_eff


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True)
