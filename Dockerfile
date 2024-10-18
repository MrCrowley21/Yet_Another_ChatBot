FROM python:3.10-slim

WORKDIR /Dinning_Hall

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "Chat.py"]