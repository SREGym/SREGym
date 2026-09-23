FROM python:3.12.11-slim-bookworm
RUN pip install --no-cache-dir psycopg[binary]==3.2.9 redis==5.2.1 requests==2.32.4 grpcio==1.74.0 grpcio-tools==1.74.0
WORKDIR /srv/platform
COPY runtime/application.py runtime/routing.py ./
COPY runtime/subscription.proto ./
RUN python -m grpc_tools.protoc -I. --python_out=. subscription.proto
ENV PYTHONUNBUFFERED=1
CMD ["python", "application.py"]
