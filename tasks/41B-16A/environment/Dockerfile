FROM saguaro-bench-base:1.0

# Agent-visible workspace. Assets are baked in under OPAQUE filenames so the
# agent can read them with its standard file-read primitive but can't tell
# which sheet is which year (or which photo is which year) from the path.
RUN mkdir -p /workspace/datasheets /workspace/photos

COPY assets/datasheets/  /workspace/datasheets/
COPY assets/photos/      /workspace/photos/
COPY brief.md instruction.md /workspace/

# Verifier-only data: ground truth + scorer + opaque->true-year map.
# Root-owned, mode 0700 so the agent user cannot read it.
COPY grade/ /grade/
RUN chmod 700 /grade && chmod 600 /grade/truth.json && chmod 700 /grade/score.py

# Create the agent user and give it the workspace.
RUN useradd -m -s /bin/bash agent && chown -R agent:agent /workspace

USER agent
WORKDIR /workspace
CMD ["/bin/bash"]
