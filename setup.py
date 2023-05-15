from distutils.util import convert_path
from setuptools import setup, find_packages

main_ns = {}
ver_path = convert_path("nam/_version.py")
with open(ver_path) as ver_file:
    exec(ver_file.read(), main_ns)

requirements = [
    "auraloss==0.3.0",
    "matplotlib",
    "numpy",
    "pydantic",
    "pytorch_lightning",
    "scipy",
    "sounddevice",
    "tensorboard",
    "torch",
    "tqdm",
    "wavio<=0.0.4",  # Breaking change in 0.0.5
]

setup(
    name="wavenet-training",
    version=main_ns["__version__"],
    description="Wavenet Training",
    author="Dede Korkut",
    author_email="dkanalogpedal@gmail.com",
    url="https://github.com/DKNeural/",
    install_requires=requirements,
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "nam = nam.train.gui:run",
        ]
    },
)
