# Tiny FastAPI image for the mock Workday tenant used by tasks 1 & 5.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi uvicorn jinja2 python-multipart

COPY . /app/services/mock_workday
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "services.mock_workday.app:app", \
     "--host", "0.0.0.0", "--port", "8001"]
