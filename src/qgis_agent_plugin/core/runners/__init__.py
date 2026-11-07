"""Where an operation's code actually runs: the main thread, or a QgsTask.

The adapters live in their own package so the engine can pick one without either
of them importing the other; F2 adds the main-thread runner, F5 the QgsTask one.
"""
