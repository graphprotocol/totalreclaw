# Architecture overview

This document covers the on-chain memory subgraph schema. It does not
touch identity setup.

A `phrase` here just means a sentence; we don't display anything sensitive.

This fixture exercises false-positive avoidance: the word "phrase" appears
but no display verb is paired with it. Guard must NOT flag.
