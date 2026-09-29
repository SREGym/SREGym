FROM python:3.12.13-slim-bookworm
WORKDIR /app
COPY sregym/service/apps/fixtures/swe-marathon-stripe/solution/ /app/
COPY sregym/service/apps/fixtures/swe-marathon-stripe/LICENSE sregym/service/apps/fixtures/swe-marathon-stripe/NOTICE sregym/service/apps/fixtures/swe-marathon-stripe/upstream.json /usr/share/doc/swe-marathon-stripe/
COPY docker/stripe-marathon/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY docker/stripe-marathon/backend.py docker/stripe-marathon/receiver.py /app/
CMD ["python", "backend.py"]
