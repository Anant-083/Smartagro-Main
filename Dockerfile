# Use an official lightweight Python image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port Hugging Face expects (7860)
EXPOSE 7860

# Run the application (adjusting for Hugging Face's required port)
CMD ["python", "app.py"]