def test_tous_les_modules_s_importent():
    """Garde-fou : un nom supprimé d'un module doit casser un test, pas le service."""
    import ambilight.__main__, ambilight.capture, ambilight.config, ambilight.ctl, ambilight.govee_razer, ambilight.traitement  # noqa: F401
