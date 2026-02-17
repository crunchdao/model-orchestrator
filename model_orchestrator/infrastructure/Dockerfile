# Step 1: Base image
ARG BASE_IMAGE=python:3.13

FROM $BASE_IMAGE
RUN apt update && apt install -y git && \
    echo "[global]" >> /etc/pip.conf && \
    echo "no-cache-dir = true" >> /etc/pip.conf && \
    echo "index-url = https://pypi.org/simple" >> /etc/pip.conf && \
    echo "extra-index-url =" >> /etc/pip.conf && \
    echo "    https://pypi.nvidia.com" >> /etc/pip.conf && \
    echo "    https://pypi.ngc.nvidia.com" >> /etc/pip.conf && \
    echo "trusted-host =" >> /etc/pip.conf && \
    echo "    pypi.nvidia.com" >> /etc/pip.conf && \
    echo "    pypi.ngc.nvidia.com" >> /etc/pip.conf

ENV PYTHONUNBUFFERED=1
ENV CATBOOST_TRAIN_DIR=/tmp/catboost_info
ENV PYCARET_CUSTOM_LOGGING_PATH=/tmp/pycaret.log

WORKDIR /workspace/submission/code/
COPY submission/code/requirements.txt ./

RUN if grep "ta-lib" requirements.txt; then \
        apt update && apt install -y gfortran libopenblas-dev liblapack-dev && \
        cd /tmp && \
        wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
        tar -xzf ta-lib-0.4.0-src.tar.gz && \
        cd ta-lib/ && \
        ./configure --prefix=/usr && \
        make && \
        make install && \
        cd /tmp && \
        rm -rf ta-lib*; \
    fi

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY submission/code/ ./
COPY resources /workspace/resources
COPY certificates /workspace/certificates

RUN ln -s /workspace/resources/ /workspace/submission/code/resources

RUN pip install "git+https://github.com/crunchdao/model-runner.git@feat/grpc-secure"


ARG HAS_GPU=False
ENV HAS_GPU=$HAS_GPU

ENTRYPOINT ["model-runner"]
CMD ["--code-directory", "/workspace/submission/code/", "--resource-directory", "/workspace/resources"]
