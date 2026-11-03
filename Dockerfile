FROM python:3.12-slim
WORKDIR /app
COPY requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt
COPY app.py ./
COPY static ./static
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
