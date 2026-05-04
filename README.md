# GenerativeConjoint

**An open-source, self-hosted conjoint survey platform with generative AI stimuli creation.**

GenerativeConjoint covers the full lifecycle of a conjoint study: survey design, D-optimal design generation, AI-generated stimuli (text and images), participant-facing survey delivery, and export of analysis-ready data.
It is built for researchers who need full control over their data and infrastructure without commercial licensing costs.

<img width="256" height="256" alt="task27_alt02_industrial_legs_medium" src="https://github.com/user-attachments/assets/559d04fa-198c-4b38-bc8f-fbc28cc5f394" />
<img width="256" height="256" alt="task21_alt02_functional_wheels_large" src="https://github.com/user-attachments/assets/01023cdc-9052-46f3-b6f5-0d73cc07b72e" />

## Features

### Survey design
- Guided step-by-step wizard with attributes, levels, participant-facing descriptions, and LLM hints
- AI-assisted attribute and level suggestion from a survey title and description (research should be theory driven!)
- Standard and blocked designs
- Heuristic based sample-size estimation
- D-optimal design generation via coordinate exchange (20 random starts)
- Drag-and-drop reordering of attributes and levels
- Multi-user collaboration (owner / collaborator roles, ORCID-based invite)

### Stimuli generation
- **Tabular** --- conventional attribute × alternative table, no AI required
- **Textual** --- LLM-generated scenario descriptions; one text per unique profile, shared across participants; level-specific LLM hints enrich generation without being shown to participants
- **Visual** --- AI-generated images (gpt-image-2, gpt-image-1, gpt-image-1-mini, or DALL-E 3); configurable quality (low / medium / high); one image per unique profile
- Per-stimulus regeneration from the Inspect Design page
- Full audit trail: prompts stored alongside outputs

### Participant experience
- Entry via unique URL with external participant key (`?key=KEY`); auto-generates an anonymous session key if no key is supplied
- Optional introduction page with consent checkbox (AI generatable)
- Randomisable attribute order (per-participant or per-task)
- Response-time tracking
- Optional auto-advance and configurable redirect URL on completion

### Results and export
- Results dashboard: completion stats and response time, part-worth utility estimates
- Shareable results link (token-protected, no login required)
- ZIP export bundle:
  - `responses.csv` --- long-format responses (one row per participant × task × alternative)
  - `design_matrix.csv` --- the fixed conjoint design
  - `design.yaml` --- full study design including attributes, levels, descriptions, and LLM hints
  - `description.md` --- human-readable data dictionary and methodology notes
  - `generated_texts.csv` / `generated_images.zip` --- all AI-generated stimuli with prompts
  - `r_starter.R` --- ready-to-run R script (clogit / mlogit examples)

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.9+, Flask 3, SQLAlchemy |
| Database | SQLite (default) or PostgreSQL |
| AI | OpenAI API (chat completions + image generation) |
| Frontend | Bootstrap 5.3, SortableJS, vanilla JS |
| Auth | ORCID OAuth 2.0 |

---

## Getting started

### 1. Clone and install

```bash
git clone https://github.com/[YOUR_USERNAME]/GenerativeConjoint.git
cd GenerativeConjoint
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Long random string for session signing |
| `DATABASE_URL` | SQLAlchemy URI (default: `sqlite:///conjoint.db`) |
| `OPENAI_API_KEY` | OpenAI API key for text and image generation |
| `ORCID_CLIENT_ID` | ORCID OAuth app client ID |
| `ORCID_CLIENT_SECRET` | ORCID OAuth app secret |
| `ORCID_REDIRECT_URI` | OAuth callback URL (e.g. `http://localhost:5000/auth/callback`) |
| `ORCID_BASE_URL` | `https://sandbox.orcid.org` (dev) or `https://orcid.org` (prod) |
| `ORCID_API_URL` | `https://pub.sandbox.orcid.org` (dev) or `https://pub.orcid.org` (prod) |

Register an OAuth application at [orcid.org/developer-tools](https://orcid.org/developer-tools) to obtain the ORCID credentials.

### 3. Run

```bash
python app.py
```

The app starts at `http://localhost:5000`.
The database is created automatically on first run; schema migrations are applied in-place on startup.

---

## Deployment (Apache + mod_wsgi)

Create `wsgi.py` in the project root:

```python
import sys, os
sys.path.insert(0, '/path/to/GenerativeConjoint')
from app import app as application
```

Add to your Apache VirtualHost:

```apache
WSGIDaemonProcess conjoint python-home=/path/to/venv \
    python-path=/path/to/GenerativeConjoint threads=4
WSGIProcessGroup conjoint
WSGIScriptAlias / /path/to/GenerativeConjoint/wsgi.py

Alias /static /path/to/GenerativeConjoint/static
```

---

## Citation

If you use GenerativeConjoint in your research, please cite:

```
Brauner, P. (2025). GenerativeConjoint: An Open-Source Conjoint Survey Platform with
Generative AI Stimuli Creation.
```
---

## Acknowledgements

Developed by [Philipp Brauner](https://orcid.org/0000-0003-2837-5181), Communication Science, RWTH Aachen University.
