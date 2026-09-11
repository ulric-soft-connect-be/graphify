shader_type spatial;

#include "res://shaders/common.gdshaderinc"
#include "res://shaders/does_not_exist.gdshaderinc"

uniform float wind_speed = 12.0;

/* A block comment naming fragment() must not register as a definition. */
vec3 surface_colour(
		float depth,
		float t) {
	// Nor must this line: fragment().
	float h = wave_height(vec2(depth, t), t);
	return mix(vec3(0.0), vec3(1.0), h);
}

void fragment() {
	ALBEDO = surface_colour(1.0, TIME);
	if (wind_speed > 0.0) {
		ALBEDO *= 2.0;
	}
}
