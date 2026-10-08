"""
WSGI config for django_backend project.

It exposes the WSGI callable as a module-level variable named application.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.management import call_command
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_backend.settings")

application = get_wsgi_application()

# Render's current service uses SQLite and does not run migrations in the
# Build Command. Run Django migrations after Django has initialized its
# application registry so the built-in auth tables and application tables
# exist on a fresh deployment.
if os.environ.get("RENDER", "").lower() == "true":
    call_command("migrate", interactive=False, verbosity=0)
