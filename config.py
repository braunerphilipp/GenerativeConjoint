import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///conjoint.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_SAMESITE = 'Lax'

    ORCID_CLIENT_ID = os.environ.get('ORCID_CLIENT_ID', '')
    ORCID_CLIENT_SECRET = os.environ.get('ORCID_CLIENT_SECRET', '')
    ORCID_REDIRECT_URI = os.environ.get('ORCID_REDIRECT_URI', 'http://localhost:5000/auth/callback')
    ORCID_BASE_URL = os.environ.get('ORCID_BASE_URL', 'https://sandbox.orcid.org')
    ORCID_API_URL = os.environ.get('ORCID_API_URL', 'https://pub.sandbox.orcid.org')

    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
