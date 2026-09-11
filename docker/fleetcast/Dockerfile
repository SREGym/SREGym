FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
COPY --from=python-dependencies constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/constraints.txt
COPY . .
EXPOSE 5000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "5000"]
