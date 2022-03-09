
FROM ubuntu:20.04

ENV HOME="/"

# Install tools and dependencies. Clean the apt metadata afterwards.
RUN apt-get update && \
    DEBIAN_FRONTEND="noninteractive" apt-get -y --no-install-recommends install \
        bison \
        build-essential \
        cmake \
        emacs \
        flex \
        git \
        less \
        libedit-dev \
        libjpeg-dev \
        libllvm7 \
        llvm-7-dev \
        libclang-7-dev \
        libelf-dev \
        libfl-dev \
        net-tools \
        openssh-client \
        software-properties-common \
        tmux \
        tree \
        vim \
        zlib1g-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install python. Clean the apt metadata afterwards. This happens separately
# because we need "add-apt-repository" from "software-properties-common".
RUN add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    DEBIAN_FRONTEND="noninteractive" apt-get -y --no-install-recommends install \
        python3.6 \
        python3.6-venv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Prepare python virtualenv.
COPY requirements.txt /requirements.txt
RUN python3.6 -m venv .venv && \
    . /.venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r /requirements.txt

# Install bcc.
RUN git clone https://github.com/iovisor/bcc.git
WORKDIR /bcc
RUN . /.venv/bin/activate && \
    git checkout v0.24.0 && \
    mkdir build && cd build && \
    cmake .. && \
    make -j "$(nproc)" && \
    make install && \
    cmake -DCMAKE_INSTALL_PREFIX="/.venv" -DPYTHON_CMD="$(which python)" .. && \
    cd src/python && \
    make -j "$(nproc)" && \
    make install

WORKDIR "$HOME"
