"""The platform-neutral half of decoding images off the UI thread (xefm#732):
the worker queue and the sizing rules both GUI backends use."""

import threading
import time

from puikit.backends import _image_loader as loader

from conftest import Inbox


# --- sizing -------------------------------------------------------------------

def test_needed_side_follows_dest_scale_and_crop():
    # 6000x4000 photo contained in 1500x1000 points on a 2x display.
    assert loader.needed_side(1500, 1000, 6000, 4000, 6000, 4000, 2.0) == 3000
    # Zoomed to its top-left quarter: four times the pixels per side... capped
    # later by the caller at full size.
    assert loader.needed_side(1500, 1000, 1500, 1000, 6000, 4000, 2.0) == 12000
    assert loader.needed_side(0, 0, 0, 0, 10, 10, 1.0) == 0


def test_wants_redecode_grows_at_once_and_shrinks_with_slack():
    entry = loader.Decoded(image=None, pixels=2000, full=6000)
    assert not loader.wants_redecode(entry, 2000)
    assert loader.wants_redecode(entry, 2001)
    assert not loader.wants_redecode(entry, 1500)
    assert loader.wants_redecode(entry, 1000)
    full = loader.Decoded(image=None, pixels=6000, full=6000)
    assert not loader.wants_redecode(full, 12000)   # nothing more to give


# --- the worker ---------------------------------------------------------------

def test_loader_serves_newest_first_and_drops_the_oldest():
    gate = threading.Event()
    order = []

    def decoder(path, side):
        gate.wait(5)
        order.append(path)
        return path

    done = []
    inbox = Inbox()
    worker = loader.ImageLoader(lambda k, r: done.append((k, r)), inbox,
                                decoder=decoder, limit=2)
    worker.submit("a", "a", 0)      # picked up at once, blocks on the gate
    time.sleep(0.05)
    for name in "bcd":
        worker.submit(name, name, 0)
    gate.set()
    inbox.run(4)
    worker.close()
    assert ("b", loader.DROPPED) in done
    assert order == ["a", "d", "c"]


def test_loader_wait_delivers_on_the_waiting_thread_once():
    done = []
    inbox = Inbox()
    worker = loader.ImageLoader(lambda k, r: done.append((k, r)), inbox,
                                decoder=lambda path, side: f"{path}@{side}")
    worker.submit("k", "p", 7)
    assert worker.wait("k", 5.0)
    assert done == [("k", "p@7")]
    inbox.run(1)                     # the posted delivery finds nothing left
    assert done == [("k", "p@7")]
    assert not worker.wait("k", 0)
    worker.close()


def test_loader_wait_gives_up_at_the_timeout():
    gate = threading.Event()
    done = []
    worker = loader.ImageLoader(lambda k, r: done.append(r), Inbox(),
                                decoder=lambda path, side: gate.wait(5))
    worker.submit("k", "p", 0)
    started = time.monotonic()
    assert not worker.wait("k", 0.02)
    assert time.monotonic() - started < 1.0
    assert done == []
    gate.set()
    worker.close()


def test_loader_reports_a_failing_decoder_as_none():
    def decoder(path, side):
        raise OSError("boom")

    done = []
    inbox = Inbox()
    worker = loader.ImageLoader(lambda k, r: done.append((k, r)), inbox,
                                decoder=decoder)
    worker.submit("k", "p", 0)
    inbox.run(1)
    worker.close()
    assert done == [("k", None)]
