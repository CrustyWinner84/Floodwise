Azure deployment instructions for the Weather App

Prerequisites:
- Azure CLI installed and logged in (`az login`)
- Docker installed locally
- An Azure subscription

Option A: Deploy via Docker image to Azure Web App for Containers (recommended)

1) Build Docker image locally:

   docker build -t <ACR_NAME>.azurecr.io/weather-app:latest .

2) Push to Azure Container Registry (ACR)

   # create resource group and ACR if needed
   az group create -n myrg -l eastus
   az acr create -n <ACR_NAME> -g myrg --sku Basic
   az acr login -n <ACR_NAME>
   docker push <ACR_NAME>.azurecr.io/weather-app:latest

3) Create Web App for Containers

   az appservice plan create -g myrg -n weatherPlan --is-linux --sku B1
   az webapp create -g myrg -p weatherPlan -n <WEBAPP_NAME> --deployment-container-image-name <ACR_NAME>.azurecr.io/weather-app:latest

4) Configure environment variables (if using Earth Engine / Google APIs)

   az webapp config appsettings set -g myrg -n <WEBAPP_NAME> --settings GOOGLE_ELEVATION_KEY="<KEY>" OPENTOPO_API_KEY="<KEY>" USE_EARTH_ENGINE=0

5) Browse to https://<WEBAPP_NAME>.azurewebsites.net

Option B: Deploy code to Azure App Service using built-in Python support
- This requires creating a zip/zip-deploy or using `az webapp up` for simple apps.

Notes:
- For Earth Engine integration, you must configure service account credentials on the server and install `earthengine-api`.
- For RTSP camera proxies, you'll need server-side services (ffmpeg) and extra infrastructure.
