"""Configuração do gunicorn para a interface web (produção em Ubuntu).

Uso::

    gunicorn -c deploy/gunicorn.conf.py imazongeo_upload.web.server:app
"""

import os

bind = f"{os.getenv('WEB_HOST', '127.0.0.1')}:{os.getenv('WEB_PORT', '5000')}"

# Os processamentos e seus logs ficam em memória no processo (dict + lock):
# mantenha UM único worker e não use max_requests, que reciclaria o worker
# e perderia os processamentos em andamento.
workers = 1
worker_class = "gthread"
threads = 8

# Envios de até 1 GB são validados antes da resposta
timeout = 900
graceful_timeout = 60

accesslog = "-"
errorlog = "-"
