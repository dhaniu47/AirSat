🚀 **[OPEN THE LIVE AIRSAT APPLICATION](https://airsat.onrender.com)**

# 🌍 AeroLytix-AI (AirSat)

AI-powered air-quality monitoring and analytics platform with an interactive pollution map, AQI visualizations, and prediction features.

## 🚀 Live Demo

**Live application:** https://airsat.onrender.com

**GitHub repository:** https://github.com/dhaniu47/AirSat

The application is deployed as a Django web service on Render. Availability and third-party air-quality data depend on the deployment and external data providers.

## ✨ Features

- Interactive India map using Leaflet.js
- AQI markers and pollution heatmap
- Current-location lookup using browser location permission
- AQI trend chart and dashboard
- Map-click AQI lookup with locality/state information
- Light, dark, and satellite map layers
- AQI prediction endpoint and analytics
- Django REST endpoints for pollution data

## 🛠 Tech Stack

- **Backend:** Python, Django 5.1, Django REST Framework
- **Frontend:** HTML, CSS, JavaScript, Leaflet.js, Chart.js
- **Data and ML:** Pandas, NumPy, scikit-learn
- **Deployment:** Render

## 📸 Screenshots

### Login, logout and password reset
![Login Logout Reset](screenshots/login_logout_reset.png)

### Light mode
![Light Mode](screenshots/light-mode.png)

### Dark mode
![Dark Mode](screenshots/dark-mode.png)

### Satellite mode
![Satellite Mode](screenshots/satellite-mode.png)

### Mobile view
![Mobile View](screenshots/mobile-view.png)

### Prediction dashboard
![Prediction](screenshots/prediction.png)

## ▶ Run Locally

From the repository root, run:

```bash
cd django_backend
python -m venv .venv
```

Activate the environment on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the required Django environment variable before starting the server. For local development in PowerShell:

```powershell
$env:DJANGO_SECRET_KEY = "replace-with-a-local-development-secret"
$env:DJANGO_DEBUG = "True"
$env:DJANGO_ALLOWED_HOSTS = "127.0.0.1,localhost"
python manage.py runserver
```

Then open http://127.0.0.1:8000.

For deployment configuration, see the Render service settings and `django_backend/.env.example`. Never commit real secrets or API tokens.

## 👩‍💻 Developer

Dhaneswari Behera

- GitHub: https://github.com/dhaniu47
- Project: https://github.com/dhaniu47/AirSat
