# Dedicated, credential-free Linux image for customer algorithm packages.
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy==2.5.1
COPY kiln_algorithm_runner.py /opt/kiln_algorithm_runner.py
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
USER 65534:65534
WORKDIR /tmp
ENTRYPOINT ["timeout", "--kill-after=3s", "100s", "python", "-IB", "/opt/kiln_algorithm_runner.py"]
