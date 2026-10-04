# Prefer simplicity over backward compatibility before v1.0.0

Before the stable v1.0.0 release, assume gwflow has no production users and choose the simplest coherent design without treating backward compatibility as a constraint. Do not add compatibility shims, migrations, or parallel legacy behavior merely to preserve earlier APIs or stored state. Backward compatibility becomes a concern at v1.0.0; pre-1.0 version numbers must not impose unnecessary design complexity.
