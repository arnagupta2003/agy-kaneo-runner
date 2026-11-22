# AGY ↔ Kaneo Runner (Docker + Dagu)

Single-container resilient runner using **Dagu** + **Antigravity CLI (`agy`)** talking directly to your self-hosted **Kaneo** board.

---

## 1. Local Dev (This Windows PC)

```powershell
cp .env.example .env
# Edit .env with KANEO_URL, KANEO_API_KEY, KANEO_PROJECT_ID, and Dagu credentials
docker compose up -d --build
docker compose exec -it agy-kaneo-runner agy   # one-time OAuth login, then /exit
docker compose exec -T agy-kaneo-runner python3 /app/worker.py check
```

Dagu Web UI: `http://localhost:8080`

---

## 2. Build & Push to Docker Hub

`.dockerignore` guarantees `.env`, `state/`, and `workspaces/` are **never** baked into the image.

```powershell
cd d:\Jetski\agy-kaneo-runner
docker login
docker build -t <your-dockerhub-user>/agy-kaneo-runner:v1 -t <your-dockerhub-user>/agy-kaneo-runner:latest .
docker push <your-dockerhub-user>/agy-kaneo-runner:v1
docker push <your-dockerhub-user>/agy-kaneo-runner:latest
```

---

## 3. Deploy on TARS (Fedora 43 Server)

1. Create the deploy directory on TARS and copy `docker-compose.prod.yml`, `.env`, `config/projects.json`, and (optionally) `state/` to reuse your OAuth login:
   ```bash
   mkdir -p /srv/agy-kaneo-runner/{state/gemini,state/config,workspaces,config}
   cd /srv/agy-kaneo-runner
   ```
2. Set ownership for the container's `runner` user (UID/GID `1000`):
   ```bash
   sudo chown -R 1000:1000 state workspaces
   chmod 600 .env
   ```
3. In `.env` on TARS, set:
   ```env
   IMAGE_NAME=<your-dockerhub-user>/agy-kaneo-runner:latest
   DAGU_BIND_IP=<TARS_TAILSCALE_IP>
   ```
4. Start the container (note `:Z` SELinux labels are already configured in `docker-compose.prod.yml` for Fedora 43):
   ```bash
   docker compose -f docker-compose.prod.yml pull
   docker compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.prod.yml exec -T agy-kaneo-runner python3 /app/worker.py check
   ```
5. If you did not copy `state/` from this PC, run the one-time OAuth login on TARS:
   ```bash
   docker compose -f docker-compose.prod.yml exec -it agy-kaneo-runner agy
   ```
6. Open Dagu UI over Tailscale at `http://<TARS_TAILSCALE_IP>:8080`.
