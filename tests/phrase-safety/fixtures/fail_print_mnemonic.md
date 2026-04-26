# Bad guidance

If the agent runs `totalreclaw setup`, the CLI will print the mnemonic
to stdout, which the agent then sees in its tool output. Bad.

The line above is the violation: "print the mnemonic" with no safety
qualifier on that same line.
