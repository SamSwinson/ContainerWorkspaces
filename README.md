# Workspaces

This is a simple Python web based API with web front end that allows you to start and stop KASM Workspace containers (I have some of the docker build files and the source files in the repo from [workspaces-images](https://github.com/kasmtech/workspaces-images)) that are hosted in the Registry you specify. The app uses Authenik to provide the username within the proxy header to the application to allow you to see containers you have started/have running it is not a secure SSO user setup but more simple solution to provide user management. The application will create a simple sqlite database so even if you restart the Python app it will still be able to track what containers have been started for users and how long they have been up. Also it uses Traefik as a proxy to get to both the Python API and the containers.

To build the API application clone the repo and in the workspaces-api directory carry out:
- docker-compose build

Then you can start 
- docker-compose up -d

For the Authenik configuration you need to add a Proxy Provider pointed towards your DNS name for the Traefik server in mode Forward auth (single application). Then attach an Application to it and assign to relevant Authentik users. Also I have added an outpost as part of my configuration as Authentik runs on a seperate server to my Python Container Workspaces server.