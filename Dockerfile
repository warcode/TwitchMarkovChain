    # Use a lightweight Python base image
    FROM python:3.10-slim-buster

    # Set the working directory in the container
    WORKDIR /app

    # Copy requirements.txt and install dependencies
    COPY requirements.txt .
    RUN pip install --no-cache-dir -r requirements.txt

    # Copy the rest of the application code
    COPY . .

    # Command to run your application
    CMD ["python", "MarkovChainBot.py"]
