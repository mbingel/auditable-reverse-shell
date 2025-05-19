# Use official Python base image
FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Copy your script and any dependencies
COPY . /app

# Install dependencies if a requirements.txt file exists
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

# Command to run your script
CMD ["python", "shell.py"]
