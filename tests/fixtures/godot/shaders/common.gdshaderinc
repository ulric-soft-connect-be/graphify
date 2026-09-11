// Shared by the shaders below.
float wave_height(vec2 p, float t) {
	return sin(p.x + t) * 0.5;
}
