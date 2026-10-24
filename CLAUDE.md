# zero-out

## Model routing

Match the model to the work, for prompting and for every delegated agent (set
`model` on the Agent tool; `/model` for the main session):

- **Haiku**: quick Q&As, syntax checks, basic sorting, light file formatting.
- **Sonnet**: standard coding, daily feature development, bug fixes, general drafting.
- **Opus**: architecture design, complex multi-file refactoring, deep research,
  strategic decisions.

If the main session is on a heavier model than the task needs, do trivial work
inline rather than spawning an agent; if it is on a lighter model than the task
needs, say so and suggest `/model` before starting.
