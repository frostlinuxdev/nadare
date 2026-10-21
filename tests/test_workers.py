# SPDX-License-Identifier: GPL-3.0-or-later


def test_wait_detached_survives_a_worker_whose_cpp_object_is_gone():
    """``finished -> deleteLater`` can destroy the C++ object while the Python
    wrapper is still in ``_detached``; calling it raises RuntimeError, which
    used to propagate out of shutdown (#48)."""
    from nadare.gui import workers

    class Gone:
        def wait(self, ms):
            raise RuntimeError("wrapped C/C++ object of type BackendWorker has been deleted")

        def isRunning(self):
            raise RuntimeError("wrapped C/C++ object of type BackendWorker has been deleted")

    class Done:
        def wait(self, ms):
            return True

        def isRunning(self):
            return False

    saved = set(workers._detached)
    workers._detached.clear()
    workers._detached.update({Gone(), Done()})
    try:
        assert workers.wait_detached(50) is True
        assert not workers._detached
    finally:
        workers._detached.clear()
        workers._detached.update(saved)
