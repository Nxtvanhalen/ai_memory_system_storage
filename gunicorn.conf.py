import os

# Binding
bind = "0.0.0.0:8080"

# Worker processes
workers = 4
worker_class = 'sync'
threads = 8
timeout = 300

# Working directory - ensure service_account.json is accessible
chdir = os.path.dirname(os.path.abspath(__file__))

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Prevent worker timeout
graceful_timeout = 120
keepalive = 5