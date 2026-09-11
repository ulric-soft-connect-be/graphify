@tool
class_name FixturePlayer
extends FixtureBase

const WATER_SHADER := "res://shaders/water.gdshader"
const BASE := preload("res://scripts/base.gd")
const MISSING := "res://scripts/does_not_exist.gd"

signal moved(distance: float)

class Tracker extends RefCounted:
	var total := 0.0

	func record(d: float) -> void:
		total += d


func _ready() -> void:
	var tracker := Tracker.new()
	tracker.record(1.0)
	_advance(2.0)
	emit_signal("moved", 2.0)


func _advance(d: float) -> void:
	var clamped := maxf(d, 0.0)
	shared_helper()
	print(clamped)
