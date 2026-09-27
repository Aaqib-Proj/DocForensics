"""QRunnable workers that wrap core/ logic for QThreadPool execution.

Workers must never touch Qt widgets directly. They communicate results
back to the GUI thread only via Qt signals.
"""
