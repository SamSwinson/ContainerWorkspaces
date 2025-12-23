#!/usr/bin/env python3
import secrets
import string
import time
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template_string, request, session, redirect
import docker
import uuid
import requests

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-change-in-production')
client = docker.from_env()

# Gitea Config - FROM DOCKER ENV
GITEA_URL = f"https://{os.getenv('GITEA_URL', 'gitea.domain.local:3002')}"
GITEA_ORG = "Hilsamlabs"
GITEA_USERNAME = os.getenv('GITEA_USERNAME', '')
GITEA_TOKEN = os.getenv('GITEA_PASSWORD', '')

# Database
DB_PATH = '/opt/workspaces-api/workspaces-sessions/workspaces_sessions.db'

# Simple SQLite DB for user sessions
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (user_id TEXT, container_name TEXT, image TEXT, password TEXT, 
                  created TIMESTAMP, ttl INTEGER, 
                  PRIMARY KEY(user_id, container_name))''')
    conn.commit()
    conn.close()

init_db()

def get_user_id():
    """Authentik sends lowercase headers"""
    return (request.headers.get('X-authentik-username') or 
            request.headers.get('X-Authentik-Username') or 
            session.get('user_id') or
            'anonymous')

def generate_secure_password(length=24):
    """Generate cryptographically secure random password"""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(chars) for _ in range(length))

def cleanup_old_containers():
    """Background cleanup for expired containers only (respect ttl)."""
    now_ts = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Select only finite sessions (ttl > 0)
    rows = c.execute(
        "SELECT container_name, created, ttl FROM sessions WHERE ttl > 0"
    ).fetchall()

    expired_names = []

    for name, created_str, ttl in rows:
        # Parse created -> timestamp
        try:
            created_dt = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
            created_ts = int(created_dt.timestamp())
        except ValueError:
            created_ts = int(created_str)

        expires_at_ts = created_ts + ttl
        if expires_at_ts <= now_ts:
            expired_names.append(name)

    # Stop and delete only expired ones
    for name in expired_names:
        try:
            container = client.containers.get(name)
            container.stop()
            print(f"Cleaned up expired container: {name}")
        except Exception:
            pass

        c.execute("DELETE FROM sessions WHERE container_name=?", (name,))

    conn.commit()
    conn.close()

def format_uptime(created_str, now):
    try:
        created = datetime.fromisoformat(created_str)
    except:
        created = datetime.fromtimestamp(int(created_str))
    uptime = now - created
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    return f"{hours}h {minutes}m"

@app.route('/images')
def get_images():
    """Fetch Workspaces images from Gitea packages (fixed endpoint + parsing)."""
    try: 
        url = f"{GITEA_URL}/api/v1/packages/{GITEA_ORG}"
        print(f"[DEBUG] Calling: {url}")
        print(f"[DEBUG] Auth: {GITEA_USERNAME}:****")
        
        resp = requests.get(
            url,
            auth=(GITEA_USERNAME, GITEA_TOKEN),
            timeout=10,
            verify=False,
        )
        print(f"[DEBUG] Status: {resp.status_code}")
        print(f"[DEBUG] Response preview: {str(resp.text[:200])}...")

        resp.raise_for_status()
        packages = resp.json()

        images = []
        seen_images = set()  # deduplicate
        
        for pkg in packages:
            name = pkg.get('name', '')
            if not name.startswith('workspaces/'):
                continue
                
            # Deduplicate by image name (multiple versions exist)
            image_key = name
            if image_key in seen_images:
                continue
            seen_images.add(image_key)
            
            display_name = name.split('/')[-1]  # 'brave'
            
            # Your JSON has "version": "latest" or "sha256:..."
            version = pkg.get('version', 'latest')
            # Prefer 'latest' tag, ignore sha256 digests
            tag = 'latest' if version == 'latest' else 'latest'
            
            images.append({
                'name': display_name,
                'image': display_name,  
                'tag': tag
            })
        
        print(f"Found {len(images)} unique workspaces images")
        return jsonify(sorted(images, key=lambda x: x['name']))
        
    except Exception as e:
        print(f"Gitea error: {e}")
        #raise
        return jsonify([
            {'name': 'brave', 'image': 'brave', 'tag': 'latest'},
            {'name': 'edge', 'image': 'edge', 'tag': 'latest'},
        ])


@app.route('/logout')
def logout():
    """Clear Flask session + redirect to correct Authentik logout"""
    session.clear()
    return redirect("https://api.workspaces.domain.local/outpost.goauthentik.io/sign_out?rd=https://api.workspaces.domain.local/")

@app.route('/api/start/<image>') 
def api_start(image):
    print(f"[START] User {get_user_id()} requested image: {image}")  
    
    user_id = get_user_id()
    if not user_id or user_id == 'anonymous':
        print(f"[START] Unauthorized: {user_id}")
        return jsonify({'error': 'Unauthorized'}), 401
    
    domain = os.getenv('DOMAIN', 'workspaces.domain.local')
    gitea_registry = os.getenv('GITEA_REGISTRY', 'gitea.domain.local:3002')
    
    full_image = f"{gitea_registry}/{image}:latest"
    raw_ttl = request.args.get('ttl')
    ttl = int(raw_ttl) if raw_ttl is not None else 0
    password = generate_secure_password()
    name = f"workspaces-{user_id}-{image.replace('/', '-')}-{uuid.uuid4().hex[:8]}"  
    
    print(f"[START] Pulling {full_image} for {name}")
    
    # Pull image
    try:
        client.images.pull(full_image)
        print(f"[START] Pulled {full_image}")
    except Exception as e:
        print(f"[START] Pull failed: {str(e)}")
        return jsonify({'error': f"Failed to pull {full_image}: {str(e)}"}), 500
    
    # Start container
    try:
        print(f"[START] Starting container {name}")
        container = client.containers.run(
            full_image,
            name=name,
            remove=True,
            detach=True,
            ports={'6901/tcp': None},
            labels={
                'traefik.enable': 'true',
                f'traefik.http.routers.{name}.rule': f"Host(`{name}.{domain}`)",
                f'traefik.http.routers.{name}.entrypoints': 'websecure',
                f'traefik.http.routers.{name}.tls': 'true',
                f'traefik.http.services.{name}.loadbalancer.server.scheme': 'https',
                f'traefik.http.services.{name}.loadbalancer.server.port': '6901',
                f'traefik.docker.network': 'proxy',
                'traefik.http.routers.{name}.middlewares': 'user-headers'
            },
            network='proxy',
        )
        print(f"[START] Started container {name} ID: {container.id[:12]}")
    except Exception as e:
        print(f"[START] Container start failed: {str(e)}")
        return jsonify({'error': f"Failed to start container: {str(e)}"}), 500
    
    # Configure VNC auth
    print(f"[START] Setting up auth for {name}")
    time.sleep(5)
    try:
        container.exec_run([
            '/bin/bash', '-c',
            f"cd /home/kasm-user && echo -e '{password}\n{password}\n' | vncpasswd -u workspaces-user -w && kill -HUP \\\$(pgrep -f kasmvnc)"
        ], privileged=True)
        print(f"[START] Auth setup complete for {name}")
    except Exception as e:
        print(f"[START] Auth setup warning: {e}")
    
    # Store in DB
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?)", 
                (user_id, name, full_image, password, datetime.now().isoformat(), ttl))
    conn.commit()
    conn.close()
    
    result = {
        'url': f"https://{name}.{domain}",
        'username': 'workspaces-user',
        'password': password,
        'ttl': ttl,
        'name': name
    }
    print(f"[START] SUCCESS: {result}")
    return jsonify(result)

@app.route('/api/list')
def api_list():
    user_id = get_user_id()  # ← Use get_user_id() consistently
    if not user_id or user_id == 'anonymous':
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT container_name, image, password, created, ttl 
        FROM sessions WHERE user_id=?
    """, (user_id,)).fetchall()
    conn.close()
    
    containers = []
    now = datetime.now()
    domain = os.getenv('DOMAIN', 'workspaces.domain.local')
    
    for row in rows:
        # FIXED: column 4 = ttl (0-based index)
        if row[4] == 0:  # INFINITE - column index FIXED
            containers.append({
                'name': row[0], 
                'image': row[1], 
                'password': row[2],
                'time_left': '∞ Infinite', 
                'infinite': True,
                'uptime': format_uptime(row[3], now),  # ← FIXED uptime
                'url': f"https://{row[0]}.{domain}",
                'expires_at': 'Never'
            })
            continue
        
        # FINITE containers
        name, image, password, created_str, ttl = row
        try:
            created = datetime.fromisoformat(created_str)
        except:
            created = datetime.fromtimestamp(int(created_str))
        
        uptime = now - created
        expires_at = created + timedelta(seconds=ttl)
        time_left = max(timedelta(0), expires_at - now)
        
        total_seconds = int(time_left.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        
        containers.append({
            'name': name,
            'image': image,
            'password': password,
            'time_left': f"{hours}h {minutes}m",
            'infinite': False,
            'uptime': f"{int(uptime.total_seconds()//3600)}h {int((uptime.total_seconds()%3600)//60)}m",  # ← FIXED
            'url': f"https://{name}.{domain}",
            'expires_at': expires_at.isoformat()
        })
    
    return jsonify({'containers': containers})

@app.route('/api/stop/<container_name>')
def api_stop(container_name):
    user_id = get_user_id()
    if not user_id or user_id == 'anonymous':
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT * FROM sessions WHERE user_id=? AND container_name=?", 
                      (user_id, container_name)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Container not found or unauthorized'}), 404
    
    try:
        container = client.containers.get(container_name)
        container.stop()
    except:
        pass
    
    conn.execute("DELETE FROM sessions WHERE user_id=? AND container_name=?", (user_id, container_name))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/toggle-autokill/<container_name>', methods=['POST'])
def toggle_autokill(container_name):
    user_id = session.get('user_id')
    is_infinite = request.json.get('infinite', True)
    
    conn = sqlite3.connect(DB_PATH)
    if is_infinite:
        conn.execute("UPDATE sessions SET ttl=0 WHERE user_id=? AND container_name=?",
                     (user_id, container_name))
        conn.commit()
        return jsonify({'success': True, 'status': 'infinite'})
    else:
        conn.execute("UPDATE sessions SET ttl=7200 WHERE user_id=? AND container_name=?",
                     (user_id, container_name))
        conn.commit()
        return jsonify({'success': True, 'status': 'finite'})

@app.route('/api/extend/<container_name>', methods=['GET', 'OPTIONS'])
def extend_session(container_name):
    if request.method == 'OPTIONS': return '', 204
    
    ttl = int(request.args.get('ttl', 7200))
    user_id = get_user_id()  # ← FIXED: Use get_user_id()
    
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute("SELECT ttl, created FROM sessions WHERE user_id=? AND container_name=?", 
                             (user_id, container_name))
        row = cursor.fetchone()
         
        if not row:
            return jsonify({'error': 'Container not found'}), 404

        current_ttl, old_created_str = row
        
        if current_ttl == 0:
            app.logger.info(f"[EXTEND] {container_name} already infinite")
            return jsonify({
                'success': True, 
                'status': 'infinite',
                'message': 'Container is already infinite'
            })

        # Convert ANY format to Unix timestamp
        try:
            created_dt = datetime.fromisoformat(old_created_str.replace('Z', '+00:00'))
            old_created = int(created_dt.timestamp())
        except ValueError:
            old_created = int(old_created_str)

        time_elapsed = int(time.time()) - old_created
        time_left = max(0, current_ttl - time_elapsed)
        new_ttl = time_left + ttl
        
        app.logger.info(f"[EXTEND] {container_name}: {time_left}s + {ttl}s = {new_ttl}s")
        conn.execute("UPDATE sessions SET ttl=? WHERE user_id=? AND container_name=?", 
                    (new_ttl, user_id, container_name))
        conn.commit()

        return jsonify({'success': True, 'old_remaining': time_left, 'added': ttl, 'new_ttl': new_ttl})

    finally:
        conn.close()

@app.route('/')
def dashboard():
    user_id = get_user_id()
    if not user_id or user_id == 'anonymous':
        return redirect('https://authentik.domain.local/if/auth/?next=https://workspaces.domain.local/')
    
    # Store in session
    session['user_id'] = user_id
    session['username'] = user_id
    
    # Get data
    list_data = api_list().get_json()
    images_data = get_images().get_json()
    
    html = '''
<!DOCTYPE html>
<html>
<head>
    <title>Workspaces Dashboard - {{ user_id }}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
    <!-- Responsive fixes for narrow screens -->
    <style>
        .container-card {
            overflow: hidden;
        }
        .card-body .container-card {
            margin-bottom: 1.5rem !important;
            display: block !important;
            padding-bottom: 0.5rem;
        }
        .card-body .container-card:last-child {
            margin-bottom: 0 !important;
        }
        .container-name {
            max-width: 250px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .container-image {
            max-width: 150px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .btn-group-sm .btn {
            white-space: nowrap;
            padding: 0.25rem 0.4rem;
            font-size: 0.75rem;
        }
        @media (max-width: 768px) {
            .container-name {
                max-width: 150px;
            }
            .btn-group-sm {
                flex-direction: column;
            }
        }
        .details-content {
            word-break: break-all;
            font-size: 0.875rem;
        }
        .btn-responsive {
    min-width: 48px;
    padding: 0.375rem 0.5rem;
    white-space: nowrap;
}

/* Desktop: Show text + icon (≥ 768px) */
@media (min-width: 768px) {
    .btn-responsive .d-none.d-sm-inline {
        display: inline-block !important;
    }
}

/* Tablet: Icon + short text (≥ 992px) */
@media (min-width: 992px) {
    .btn-responsive .d-none.d-md-inline {
        display: inline-block !important;
    }
}

/* Mobile: Icons only (< 768px) */
@media (max-width: 767.98px) {
    .btn-group {
        flex-direction: row !important;
    }
    .btn-responsive {
        padding: 0.25rem 0.375rem;
        font-size: 0.75rem;
    }
}

    </style>
</head>
<body class="bg-light">
    <!-- NAVBAR -->
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
      <div class="container-fluid">
        <span class="navbar-text me-3">
          Logged in as: <strong>{{ user_id }}</strong>
        </span>
        <a href="https://api.workspaces.domain.local/logout" class="btn btn-outline-danger">Logout</a>
      </div>
    </nav>

    <div class="container mt-4">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h1><i class="bi bi-boxes me-2"></i>Workspaces Dashboard</h1>
        </div>
        
        <div class="row">
            <div class="col-lg-4 mb-4">
                <div class="card shadow">
                    <div class="card-header">
                        <h5 class="mb-0"><i class="bi bi-play-circle me-2"></i>Start New Container</h5>
                    </div>
                    <div class="card-body">
                        <form id="startForm">
                            <div class="mb-3">
                                <label class="form-label">Image</label>
                                <select name="image" class="form-select" id="imageSelect">
                                    {% for img in images_data %}
                                    <option value="{{ img.image }}">{{ img.name }} ({{ img.tag }})</option>
                                    {% endfor %}
                                </select>
                            </div>
                            <div class="form-check mb-3">
                                <input type="checkbox" class="form-check-input" name="ttl" id="ttl" checked>
                                <label class="form-check-label" for="ttl">Auto-kill after 2 hours</label>
                            </div>
                            <button type="submit" class="btn btn-primary w-100">
                                <i class="bi bi-rocket-takeoff"></i> Launch Container
                            </button>
                        </form>
                    </div>
                </div>
            </div>

<div class="col-lg-8">
    <div class="card shadow">
        <div class="card-header">
            <h5 class="mb-0">
                <i class="bi bi-laptop me-2"></i>Your Containers ({{ list_data.containers|length }})
            </h5>
        </div>

        <div class="card-body">
            {% if list_data.containers %}
                <div class="d-flex flex-column gap-1">
                    {% for container in list_data.containers %}
                    <div class="border rounded-3 p-2 container-card border-{% if container.infinite %}success{% elif container.time_left.split(' ')[0] != '0h' %}success{% else %}danger{% endif %}">
                        <!-- Main display: Short title + uptime + buttons -->
                        <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
                            <div class="flex-grow-1 min-width-0">
                                <!-- Short title: image + unique ID only -->
                                <h6 class="card-title mb-1 container-name" title="{{ container.name }}">
                                    {{ container.image.split('/')[-1] }} • {{ container.name.split('-')[-1] }}
                                </h6>
                                <small class="text-muted d-block">
                                    <i class="bi bi-clock-history me-1"></i>Uptime: {{ container.uptime }}
                                    {% if container.infinite %}
                                        | <span class="text-success fw-bold">∞ Infinite</span>
                                    {% elif container.time_left.split(' ')[0] != '0h' %}
                                        | <i class="bi bi-hourglass-split me-1"></i>{{ container.time_left }}
                                    {% else %}
                                        | <span class="text-danger">
                                            <i class="bi bi-exclamation-triangle me-1"></i>EXPIRED
                                          </span>
                                    {% endif %}
                                </small>
                            </div>
                            <div class="btn-group btn-group-sm flex-shrink-0 ms-2 d-flex" role="group">
                                <a href="{{ container.url }}" target="_blank" 
                                   class="btn btn-outline-success btn-responsive" title="Connect">
                                    <i class="bi bi-box-arrow-up-right d-none d-sm-inline me-1"></i>
                                    <span class="d-none d-md-inline">Connect</span>
                                    <i class="bi bi-box-arrow-up-right d-md-none"></i>
                                </a>
                                <button onclick="extend('{{ container.name }}', 7200)" 
                                        class="btn btn-outline-warning btn-responsive" title="Extend Timer">
                                    <i class="bi bi-clock d-none d-sm-inline me-1"></i>
                                    <span class="d-none d-md-inline">+2h</span>
                                    <i class="bi bi-clock d-md-none"></i>
                                </button>
                                <button onclick="stop('{{ container.name }}')" 
                                        class="btn btn-outline-danger btn-responsive" title="Stop Container">
                                    <i class="bi bi-stop d-none d-sm-inline me-1"></i>
                                    <span class="d-none d-md-inline">Stop</span>
                                    <i class="bi bi-stop d-md-none"></i>
                                </button>
                            </div>
                        </div>

                        <!-- Credentials dropdown -->
                        <details class="mt-2">
                            <summary class="btn-link small p-0">Show Credentials</summary>
                            <div class="mt-2 p-2 bg-light rounded details-content">
                                <div class="d-flex align-items-center mb-2">
                                    <strong>Username:</strong> 
                                    <span class="ms-2">workspaces-user</span>
                                </div>
                                <div class="d-flex align-items-center">
                                    <strong>Password:</strong> 
                                    <span class="ms-2 flex-grow-1" style="word-break: break-all;">
                                        {{ container.password }}
                                    </span>
                                    <button class="btn btn-sm btn-outline-secondary ms-1" 
                                            onclick="copyPassword(this, '{{ container.password }}')"
                                            title="Copy password">
                                        <i class="bi bi-clipboard"></i>
                                    </button>
                                </div>
                            </div>
                        </details>
                        
                        <!-- Container Details dropdown -->
                        <details class="mt-2">
                            <summary class="btn-link small p-0">
                                <i class="bi bi-info-circle me-1"></i>Container Details
                            </summary>
                            <div class="mt-2 p-2 bg-light rounded details-content">
                                <div class="row g-2 small">
                                    <div class="col-6">
                                        <strong>Full Name:</strong><br>
                                        <span class="text-muted" style="word-break: break-all;">
                                            {{ container.name }}
                                        </span>
                                    </div>
                                    <div class="col-6">
                                        <strong>Image:</strong><br>
                                        <span class="text-muted">{{ container.image }}</span>
                                    </div>
                                    <div class="col-6">
                                        <strong>Connect URL:</strong><br>
                                        <a href="{{ container.url }}" target="_blank" 
                                           class="text-decoration-none">{{ container.url }}</a>
                                    </div>
                                    <div class="col-6">
                                        <strong>Expires:</strong><br>
                                        <span class="text-muted">{{ container.expires_at }}</span>
                                    </div>
                                </div>
                            </div>
                        </details>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div class="text-center py-5">
                    <i class="bi bi-inboxes display-1 text-muted mb-3"></i>
                    <h5 class="text-muted">No running containers</h5>
                    <p class="text-muted">Start a new container!</p>
                </div>
            {% endif %}
        </div>
    </div>
</div>
            

    <script>
document.getElementById('startForm').onsubmit = async (e) => {
    e.preventDefault();
    
    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn.innerHTML;
    
    // Show loading state
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<div class="spinner-border spinner-border-sm me-2" role="status"></div>Launching...';
    
    const formData = new FormData(e.target);
    const image = formData.get('image');
    const ttl = formData.get('ttl') ? 'ttl=7200' : '';
    
    console.log(`Launching image: ${image} ttl: ${ttl}`); // Browser console debug
    
    try {
        const response = await fetch(`/api/start/${encodeURIComponent(image)}?${ttl}`);
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }
        
        const result = await response.json();
        console.log('Success:', result);
        
        // Show success with connect URL
        alert(`Container launched!\nConnect at: ${result.url}\nUsername: ${result.username}\nPassword: ${result.password}`);
        location.reload();
        
    } catch (err) {
        console.error('Launch failed:', err);
        alert(`Failed to start container: ${err.message}`);
    } finally {
        // Reset button
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
    }
};

async function stop(name) {
    if (confirm('Stop this container?')) {
        try {
            await fetch(`/api/stop/${encodeURIComponent(name)}`);
            location.reload();
        } catch (err) {
            alert('Failed to stop container: ' + err);
        }
    }
}

async function extend(name, ttl) {
    const button = event.target;
    const originalHTML = button.innerHTML;

    button.disabled = true;
    button.innerHTML = '<i class="bi bi-hourglass-split spinner-border spinner-border-sm me-1"></i> Extending...';

    try {
        const response = await fetch(`/api/extend/${encodeURIComponent(name)}?ttl=${ttl}`);

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }

        const result = await response.json();
        console.log('Extend success:', result);

        if (result.status === 'infinite') {
            // Already infinite – show that instead of "Extended!"
            button.innerHTML = '<i class="bi bi-infinity text-success me-1"></i> Already infinite';
            button.classList.remove('btn-outline-warning');
            button.classList.add('btn-success');
        } else {
            // Normal finite extend case
            button.innerHTML = '<i class="bi bi-check-lg text-success me-1"></i> Extended!';
            button.classList.add('btn-success');

            setTimeout(() => {
                location.reload();
            }, 1000);
        }

    } catch (err) {
        console.error('Extend failed:', err);
        button.innerHTML = originalHTML;
        button.classList.remove('btn-success');
        alert(`Extend failed: ${err.message}`);
    } finally {
        button.disabled = false;
    }
}
async function copyPassword(button, password) {
    try {
        await navigator.clipboard.writeText(password);
        const originalIcon = button.innerHTML;
        button.innerHTML = '<i class="bi bi-check-lg text-success"></i>';
        button.classList.remove('btn-outline-secondary');
        button.classList.add('btn-success');
        setTimeout(() => {
            button.innerHTML = originalIcon;
            button.classList.remove('btn-success');
            button.classList.add('btn-outline-secondary');
        }, 2000);
    } catch (err) {
        // Fallback for older browsers
        const textArea = document.createElement('textarea');
        textArea.value = password;
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        alert('Password copied!');
    }
}
</script>
            </div>
            </div>
        </div>
    </div>
    
    
</body>
</html>
    '''
    return render_template_string(html, user_id=user_id, list_data=list_data, images_data=images_data)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'user': get_user_id()})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
