FROM python:3.12-slim
RUN pip install flask docker
WORKDIR /app
COPY app/app.py /app/app.py
EXPOSE 5000
CMD ["python", "app.py"]
