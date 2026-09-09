# Serve para Hugging Face Spaces, Render e Koyeb.
# O Spaces exige a porta 7860; o Render e o Koyeb injetam $PORT.
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

ENV PORT=7860
EXPOSE 7860

# A chave nunca entra na imagem. Define YOUTUBE_API_KEY nos secrets da plataforma:
#   Hugging Face -> Settings -> Variables and secrets
#   Render/Koyeb -> Environment variables
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
