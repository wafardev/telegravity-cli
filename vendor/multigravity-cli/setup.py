from setuptools import setup

setup(
    name="multigravity-cli",
    version="1.0.0",
    description="Multi-profile session and quota manager for Antigravity CLI (agy)",
    scripts=["bin/mgy", "bin/mgy-quota"],
    python_requires=">=3.8",
)
