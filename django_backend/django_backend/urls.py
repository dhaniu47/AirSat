from django.contrib import admin
from django.urls import path

from pollution import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.index, name="index"),
    path("healthz/", views.health_check, name="health-check"),
    path("api/login/", views.login_view, name="api-login"),
    path("api/logout/", views.logout_view, name="api-logout"),
    path("api/register/", views.register, name="api-register"),
    path("api/reset-password/", views.reset_password, name="api-reset-password"),
    path("api/data/", views.get_pollution_data, name="api-data"),
    path("save/", views.save_data, name="save"),
    path("pollution/", views.pollution, name="pollution"),
    path("api/cpcb-stations/", views.cpcb_stations, name="cpcb-stations"),
    path("api/predict/", views.predict_api, name="api-predict"),
    path("analytics_data/", views.analytics_data, name="analytics-data"),
    path("tiles/", views.tiles, name="tiles"),
    path("history/", views.history, name="history"),
    path("dashboard/", views.dashboard, name="dashboard"),
]
