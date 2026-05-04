import secrets as _secrets
import string as _string

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


def _gen_public_id() -> str:
    """8-character random lowercase-alphanumeric ID for the external-facing entry URL."""
    return ''.join(_secrets.choice(_string.ascii_lowercase + _string.digits) for _ in range(8))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    orcid = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(200))
    email = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    memberships = db.relationship('SurveyUser', back_populates='user', lazy='dynamic')


class Survey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(8), unique=True, default=_gen_public_id)
    title = db.Column(db.String(200), nullable=False)
    language = db.Column(db.String(100), default='English')
    intro_text = db.Column(db.Text)
    show_intro = db.Column(db.Boolean, default=True)
    auto_advance = db.Column(db.Boolean, default=False)
    description = db.Column(db.Text)
    presentation_type = db.Column(db.String(20), default='tabular')  # tabular | textual | visual
    num_tasks = db.Column(db.Integer, default=8)
    num_alternatives = db.Column(db.Integer, default=2)
    pool_size = db.Column(db.Integer)        # None = standard; N > num_tasks = blocked design
    has_duplicate_profiles = db.Column(db.Boolean, default=False)
    duplicate_profile_count = db.Column(db.Integer, default=0)
    randomise_attrs = db.Column(db.Boolean, default=False)  # legacy; superseded by attr_order_mode
    attr_order_mode = db.Column(db.String(20), default='none')  # none | participant | task
    redirect_url = db.Column(db.String(1000))  # contains %KEY% placeholder
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    design_generated = db.Column(db.Boolean, default=False)
    design_stale = db.Column(db.Boolean, default=True)
    d_efficiency = db.Column(db.Float)
    text_prompt = db.Column(db.Text)          # prompt template for textual presentation
    texts_generated = db.Column(db.Boolean, default=False)
    image_prompt = db.Column(db.Text)         # base prompt for DALL-E image generation
    image_model = db.Column(db.String(50), default='gpt-image-2')
    image_quality = db.Column(db.String(20), default='medium')
    images_generated = db.Column(db.Boolean, default=False)
    results_token = db.Column(db.String(24), unique=True)

    attributes = db.relationship('Attribute', back_populates='survey',
                                 order_by='Attribute.order_index', cascade='all, delete-orphan')
    memberships = db.relationship('SurveyUser', back_populates='survey', cascade='all, delete-orphan')
    participants = db.relationship('Participant', back_populates='survey', lazy='dynamic')
    design_tasks = db.relationship('DesignTask', back_populates='survey',
                                   cascade='all, delete-orphan', lazy='dynamic')
    generated_texts = db.relationship('GeneratedText', back_populates='survey',
                                      cascade='all, delete-orphan', lazy='dynamic')
    generated_images = db.relationship('GeneratedImage', back_populates='survey',
                                       cascade='all, delete-orphan', lazy='dynamic')

    def get_entry_url(self, base_url):
        return f"{base_url.rstrip('/')}/s/{self.public_id}"

    @property
    def owner_id(self):
        owner = next((m for m in self.memberships if m.role == 'owner'), None)
        return owner.user_id if owner else self.created_by


class PendingInvite(db.Model):
    """ORCID-based invite for a user who has not yet registered."""
    __tablename__ = 'pending_invite'
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    orcid = db.Column(db.String(20), nullable=False)
    invited_at = db.Column(db.DateTime, default=datetime.utcnow)

    survey = db.relationship('Survey')

    __table_args__ = (
        db.UniqueConstraint('survey_id', 'orcid', name='uq_pending_invite'),
    )


class SurveyUser(db.Model):
    __tablename__ = 'survey_user'
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role = db.Column(db.String(20), default='owner')      # owner | collaborator
    status = db.Column(db.String(20), default='active')   # active | archived | left
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    survey = db.relationship('Survey', back_populates='memberships')
    user = db.relationship('User', back_populates='memberships')


class Attribute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    order_index = db.Column(db.Integer, default=0)

    survey = db.relationship('Survey', back_populates='attributes')
    levels = db.relationship('Level', back_populates='attribute',
                             order_by='Level.order_index', cascade='all, delete-orphan')


class Level(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attribute_id = db.Column(db.Integer, db.ForeignKey('attribute.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    llm_hint = db.Column(db.Text)   # extra context sent to the LLM when generating textual stimuli
    order_index = db.Column(db.Integer, default=0)

    attribute = db.relationship('Attribute', back_populates='levels')


class DesignTask(db.Model):
    """Pre-generated cell in the conjoint design matrix."""
    __tablename__ = 'design_task'
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    task_index = db.Column(db.Integer, nullable=False)
    alternative_index = db.Column(db.Integer, nullable=False)
    # JSON: {"<attribute_id>": <level_id>, ...}
    profile = db.Column(db.Text, nullable=False)

    survey = db.relationship('Survey', back_populates='design_tasks')


class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    external_key = db.Column(db.String(500), nullable=False)
    prolific_pid = db.Column(db.String(500))
    study_id = db.Column(db.String(500))
    session_id = db.Column(db.String(500))
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    current_task = db.Column(db.Integer, default=0)
    task_assignment = db.Column(db.Text)     # JSON list of design task indices for blocked designs

    survey = db.relationship('Survey', back_populates='participants')
    responses = db.relationship('Response', back_populates='participant',
                                cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('survey_id', 'external_key', name='uq_participant_key'),
    )


class GeneratedText(db.Model):
    """AI-generated textual stimulus for one unique attribute-level profile."""
    __tablename__ = 'generated_text'
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    task_index = db.Column(db.Integer)           # first-occurrence reference (informational)
    alternative_index = db.Column(db.Integer)    # first-occurrence reference (informational)
    text = db.Column(db.Text, nullable=False)
    prompt = db.Column(db.Text)
    profile_key = db.Column(db.String(500), nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    survey = db.relationship('Survey', back_populates='generated_texts')

    __table_args__ = (
        db.UniqueConstraint('survey_id', 'profile_key', name='uq_generated_text_profile'),
    )


class GeneratedImage(db.Model):
    """AI-generated image for one unique attribute-level profile."""
    __tablename__ = 'generated_image'
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('survey.id'), nullable=False)
    task_index = db.Column(db.Integer)           # first-occurrence reference (informational)
    alternative_index = db.Column(db.Integer)    # first-occurrence reference (informational)
    image_data = db.Column(db.LargeBinary, nullable=False)  # full-res PNG (1024×1024)
    thumb_data = db.Column(db.LargeBinary, nullable=False)  # downscaled PNG (400×400)
    prompt = db.Column(db.Text)
    profile_key = db.Column(db.String(500), nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    survey = db.relationship('Survey', back_populates='generated_images')

    __table_args__ = (
        db.UniqueConstraint('survey_id', 'profile_key', name='uq_generated_image_profile'),
    )


class Response(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey('participant.id'), nullable=False)
    task_index = db.Column(db.Integer, nullable=False)
    chosen_alternative = db.Column(db.Integer, nullable=False)  # 0-indexed
    responded_at = db.Column(db.DateTime, default=datetime.utcnow)
    response_time_ms = db.Column(db.Integer)   # ms from task page load to submission
    # JSON snapshot of the full choice set at response time (audit trail)
    choice_set = db.Column(db.Text)

    participant = db.relationship('Participant', back_populates='responses')
