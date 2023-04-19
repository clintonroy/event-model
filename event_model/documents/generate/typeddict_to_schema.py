# type: ignore

import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple, Type, Union

from pydantic import BaseConfig, BaseModel, Field, create_model
from pydantic.fields import FieldInfo
from collections import OrderedDict
from typing_extensions import (
    Annotated,
    NotRequired,
    _TypedDictMeta,
    get_args,
    get_origin,
)

from event_model import SCHEMA_PATH
from event_model.documents import (
    Datum,
    DatumPage,
    Event,
    EventDescriptor,
    EventPage,
    Resource,
    RunStart,
    RunStop,
    StreamDatum,
    StreamResource,
)
from event_model.documents.generate.type_wrapper import (
    ALLOWED_ANNOTATION_ELEMENTS,
    AsRef,
    extra_schema,
)

# The hacky indexing on types isn't possible with python < 3.9
if sys.version_info[:2] < (3, 9):
    raise EnvironmentError("schema generation requires python 3.8 or higher")


SCHEMA_OUT_DIR = Path("event_model") / SCHEMA_PATH


# Used to add user written schema to autogenerated schema.
def merge_dicts(dict1: dict, dict2: dict) -> dict:
    """
    Takes two dictionaries with subdirectories and returns a new dictionary of
    the two merged:
    dict1 = {
        "x1": {
            "y1": 0,  "y3": {"z1" : [1, 2], "z2": 1}
        },
        "x2" : 0,
        "x3": 1
    }
    and
    dict2 = {
        "x1": {
            "y2" : 0,  "y3": {"z1": [3, 4], "z3": 5}
        },
        "x3" : 0
    }
    returns
    {
        "x1": {
            "y1": 0, "y2": 0,  "y3": {"z1": [1, 2, 3, 4], "z2": 1, "z3": 5}
        },
        "x2": 0
        "x3": 1
    }
    """

    return_dict = dict2.copy()

    for key in dict1:
        if key not in dict2:
            return_dict[key] = dict1[key]

        elif not isinstance(dict1[key], type(dict2[key])):
            return_dict[key] = dict1[key]

        elif isinstance(dict1[key], dict):
            return_dict[key] = merge_dicts(dict1[key], dict2[key])

        elif isinstance(dict1[key], list):
            return_dict[key] = dict1[key] + dict2[key]

    return return_dict


def format_all_annotations(
    typed_dict: _TypedDictMeta, new_basemodel_classes: Dict[str, BaseModel] = {}
) -> _TypedDictMeta:
    """Goes through all field type annotations and formats them to be acceptable
    to pydantic.

    It formats annotations in the following way:
    * NotRequired[Annotated[X, FieldInfo, ...] -> Annotated[Optional[X],
      FieldInfo, ...]
    * Annotated[X, AsRef("ref_name"), FieldInfo] ->
        Annotated[ref_name_BaseModel_containing_X, FieldInfo]
    * If X is also a typeddict then Annotated[X, FieldInfo] ->
        Annotated[parse_typeddict_to_schema(X), FieldInfo]

    Parameters
    -----------------
    typed_dict : dict
        TypedDict to change annotations of prior to being converted to BaseModel.

    new_basemodel_classes : dict
        Dict where keys are names of _TypedDictMeta classes referenced inside
        the TypedDict being converted to a BaseModel, and values are BaseModel
        classes that have already been generated from those TypedDicts -
        pydantic will throw an error if we try to generate the same BaseModel
        twice so we just reference the one already created if multiple
        annotations have the same TypedDict.

    Returns
    -----------------
    typed_dict : dict
        The same dict with field annotations adjusted to be pydantic friendly.

    """
    new_annotations = {}

    for field_name, field_type in typed_dict.__annotations__.items():
        new_annotations[field_name] = field_parser(
            field_name, field_type, new_basemodel_classes
        )

    typed_dict.__annotations__ = new_annotations

    return typed_dict


def field_is_annotated(field: type) -> bool:
    """Returns True if a field is of type Annotated[X]."""
    return get_origin(field) == Annotated


def field_is_not_required(
    field_type: type, remove_origin_if_NotRequired: bool = False
) -> Tuple[type, bool]:
    """Checks if a field is of type is NotRequired[X].

    Parameters
    -----------------
    field_type : dict
        Field type taken from an annotation.
    remove_origin_if_NotRequired : bool
        If True returns the inputted field_type with NotRequired stripped off.

    Returns
    -----------------
    (field_type, is_not_required) : Tuple[type, bool]
        is_not_required is True if the field is of type NotRequired[X].
        field_type is the same as the inputted field_type however if
        remove_origin_if_NotRequired is true it will strip NotRequired.
    """
    is_not_required = get_origin(field_type) == NotRequired
    if is_not_required and remove_origin_if_NotRequired:
        args = get_args(field_type)
        assert len(args) == 1
        field_type = args[0]
    return field_type, is_not_required


def get_field_type(
    field_name: str,
    field_type: type,
    new_basemodel_classes: Dict[str, BaseModel],
) -> type:
    """Goes through an annotation and finds the type it represents without the
    additional context - e.g int inside Annotated[int, Field(description=""),
    AsRef("blah")].
    It is assumed that either the field origin is Annotated, or the field has no
    origin.

    Parameters
    ----------------
    field_name : str
        Name of the field being parsed.
    field_type : type
        Annotation to be parsed.
    new_basemodel_classes : dict
        Dict where keys are names of _TypedDictMeta classes referenced inside
        the TypedDict being converted to a BaseModel, and values are BaseModel
        classes that have already been generated from those TypedDicts -
        pydantic will throw an error if we try to generate the same BaseModel
        twice so we just reference the one already created if multiple
        annotations have the same TypedDict.

    Returns
    -----------------
    field_type : type
        The data type inside the annotation seperate from the other context in
        the annotation.
    """
    args = get_args(field_type)
    if args:
        field_type = [
            x
            for x in args
            if True not in [
                isinstance(x, y) for y in ALLOWED_ANNOTATION_ELEMENTS
            ]
        ]
        assert len(field_type) == 1, (
            f'Field "{field_name}" has multiple types: '
            f'{"and ".join([x.__class__.__name__ for x in field_type])}'
        )
        field_type = field_type[0]

    # If the TypedDict references another TypedDict then another
    # BaseModel is recursively generated from that TypedDict,
    # and the field annotation is swapped to that BaseModel.
    field_type = change_sub_typed_dicts_to_basemodels(
        field_type, new_basemodel_classes
    )

    return field_type


def get_annotation_contents(field_type: type) -> Tuple[Optional[AsRef], FieldInfo]:
    """Goes through the args of an Annotation and parses out any AsRef(), or
    FieldInfo.

    Parameters
    -----------------
    field_type : type
        Annotation to be parsed.

    Returns
    -----------------
    (as_ref, field_info) : Tuple[AsRef, FieldInfo]
        as_ref is the AsRef tag in the annotation, or None if
        there is no AsRef tag. field_info is the FieldInfo class returned from
        the Field() call in the Annotation, or an empty Field() call if none is found.
    """

    args = get_args(field_type)
    as_ref = None
    field_info = Field()
    if args:
        for arg in args:
            if isinstance(arg, AsRef):
                as_ref = arg
            elif isinstance(arg, FieldInfo):
                field_info = arg

    return as_ref, field_info


def parse_AsRef(
    field_type: type,
    field_info: FieldInfo,
    as_ref: AsRef,
    new_basemodel_classes: Dict[str, BaseModel],
) -> type:
    """Parses the AsRef tag and makes a new BaseModel class containing a
    __root__ of the field with the AsRef.

    Parameters
    -----------------
    field_type : type
        Datatype to put in the __root__ field of the generated basemodel.
    field_info : FieldInfo
        Info included in the Field() of the annotation.
    as_ref : AsRef
        AsRef tag in the annotation.
    new_basemodel_classes : dict
        Dict where keys are names of a basemodel class generated by this
        function, and values are the class itseldf. If another field has the
        same AsRef the already generated baseclass in this dict is used as the
        new field value.

    Returns
    -----------------
    field_type : type
        The generated basemodel.
    """
    ref_field_info = Field()
    # Regex is extracted from the field info and placed in the new field info
    if field_info.regex:
        ref_field_info.regex = field_info.regex
    ref_field_type = Annotated[field_type, ref_field_info]

    if as_ref.ref_name not in new_basemodel_classes:
        field_type = create_model(
            as_ref.ref_name,
            __config__=Config,
            __root__=(ref_field_type, None),
        )
        new_basemodel_classes[as_ref.ref_name] = field_type
    else:
        generated_basemodel_type = new_basemodel_classes[
            as_ref.ref_name
        ].__annotations__["__root__"]
        assert get_args(ref_field_type)[0] == get_args(generated_basemodel_type)[0], (
            f'Fields with type AsRef("{as_ref.ref_name}") have differing types: '
            f"{generated_basemodel_type} and {ref_field_type}"
        )
        field_type = new_basemodel_classes[as_ref.ref_name]

    return field_type


def change_sub_typed_dicts_to_basemodels(
    field_type: type, new_basemodel_classes: Dict[str, BaseModel]
) -> dict:
    """Checks for any TypedDicts in the field_type and converts them to
    basemodels.

    Parameters
    -----------------
    field_type : type
        Annotation to be parsed.
    new_basemodel_classes : dict
        Dict where keys are names of _TypedDictMeta classes referenced inside
        the TypedDict being converted to a BaseModel, and values are BaseModel
        classes that have already been generated from those TypedDicts -
        pydantic will throw an error if we try to generate the same BaseModel
        twice so we just reference the one already created if multiple
        annotations have the same TypedDict.

    Returns
    -----------------
    field_type : type
        New field type with TypedDicts swapped to basemodels
    """
    if isinstance(field_type, _TypedDictMeta):
        if field_type.__name__ not in new_basemodel_classes:
            field_type = parse_typeddict_to_schema(
                field_type,
                return_basemodel=True,
                new_basemodel_classes=new_basemodel_classes,
            )
            new_basemodel_classes[field_type.__name__] = field_type
        else:
            field_type = new_basemodel_classes[field_type.__name__]
    # It's still possible there's a TypedDict in the args to be converted - e.g
    # Dict[str, SomeTypedDict]
    else:
        origin = get_origin(field_type)
        args = get_args(field_type)
        if origin and args:
            field_type = origin[
                tuple(
                    change_sub_typed_dicts_to_basemodels(arg, new_basemodel_classes)
                    for arg in args
                )
            ]
    return field_type


def field_parser(
    field_name: str, field_type: type, new_basemodel_classes: Dict[str, BaseModel]
):
    """Parses a field annotation and generates a pydantic friendly one by
    extracting relevant information.

    Parameters
    ----------------
    field_name : str
        Name of the field being parsed.
    field_type : type
        Annotation to be parsed.
    new_basemodel_classes : dict
        Dict where keys are names of _TypedDictMeta classes referenced inside
        the TypedDict being converted to a BaseModel, and values are BaseModel
        classes that have already been generated from those TypedDicts -
        pydantic will throw an error if we try to generate the same BaseModel
        twice so we just reference the one already created if multiple
        annotations have the same TypedDict.

    Returns
    -----------------
    field_info : type
        The field_type inputted, but parsed to be pydantic friendly
    """
    field_type, is_not_required = field_is_not_required(
        field_type, remove_origin_if_NotRequired=True
    )

    as_ref, field_info = get_annotation_contents(field_type)

    field_type = get_field_type(
        field_name,
        field_type,
        new_basemodel_classes,
    )

    if as_ref:
        field_type = parse_AsRef(field_type, field_info, as_ref, new_basemodel_classes)
        field_info.regex = None

    if is_not_required:
        field_type = Optional[field_type]

    return Annotated[field_type, field_info]


class Config(BaseConfig):
    """Config for generated BaseModel."""

    def alias_generator(string_to_be_aliased):
        """Alias in snake case"""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", string_to_be_aliased).lower()


def strip_newline_literal(schema: dict):
    """
    Pydantic formats the docstring newlines by literally including `\n` in the
    description. To avoid this uglyness this function swaps `\n` for ` ` in
    every "description` key in the schema.
    """
    for key in schema:
        if isinstance(schema[key], dict):
            schema[key] = strip_newline_literal(schema[key])
        elif key == "description":
            schema[key] = schema[key].replace("\n", " ")
    return schema


def sort_jsonschema(schema: dict) -> dict:
    """Sorts the schema properties keys alphabetically by key name, exchanging the
    properties dicts for OrderedDicts"""
    for key in schema:
        if key == "properties":
            schema[key] = OrderedDict(
                sorted(list(schema[key].items()), key=lambda x: x[0])
            )
        elif isinstance(schema[key], dict):
            schema[key] = sort_jsonschema(schema[key])
    return schema
        

# From https://github.com/pydantic/pydantic/issues/760#issuecomment-589708485
def parse_typeddict_to_schema(
    typed_dict: _TypedDictMeta,
    out_dir: Optional[Path] = None,
    return_basemodel: bool = False,
    new_basemodel_classes: Dict[str, BaseModel] = {},
    sort: bool = True
) -> Union[Type[BaseModel], Dict[str, type]]:
    """Takes a TypedDict and generates a jsonschema from it.

    Parameters:
    ---------------------------
    typed_dict : _TypedDictMeta
        The typeddict to be converted to a pydantic basemodel.
    out_dir: Optional[Path]
        Optionally provide a directory to store the generated json schema from
        the basemodel, if None then the dictionary schema won't be saved
        to disk.
    return_basemodel : bool
        Optionally return the basemodel as soon as it's generated, rather than
        converting it to a dictionary. Required for converting TypedDicts
        within documents.
    new_basemodel_classes : Dict[str, BaseModel]
        Optionally provide basemodel classes already generated during a
        conversion. Required for when the function is called recursively.
    sort : bool
        If true, sort the properties keys in the outputted schema.

    Returns
    --------------------------------------------------------------------------
    Either the generated BaseModel or the schema dictionary generated from it,
    depending on if return_basemodel is True.
    """
    annotations: Dict[str, type] = {}

    typed_dict = format_all_annotations(
        typed_dict, new_basemodel_classes=new_basemodel_classes
    )

    for name, field in typed_dict.__annotations__.items():
        default_value = getattr(typed_dict, name, ...)
        annotations[name] = (field, default_value)

    model = create_model(typed_dict.__name__, __config__=Config, **annotations)

    # Docstring is used as the description field.
    model.__doc__ = typed_dict.__doc__

    if return_basemodel:
        return model

    # title goes to snake_case
    model.__name__ = Config.alias_generator(typed_dict.__name__).lower()

    model_schema = model.schema(by_alias=True)
    model_schema = strip_newline_literal(model_schema)

    # Add the manually defined extra stuff
    if typed_dict in extra_schema:
        model_schema = merge_dicts(extra_schema[typed_dict], model_schema)

    if sort:
        model_schema = sort_jsonschema(model_schema)

    if out_dir:
        with open(out_dir / f'{model_schema["title"]}.json', "w+") as f:
            json.dump(model_schema, f, indent=3)

    return model_schema


def generate_all_schema(schema_out_dir: Path = SCHEMA_OUT_DIR) -> None:
    """Generates all schema in the documents directory."""
    parse_typeddict_to_schema(DatumPage, out_dir=schema_out_dir)
    parse_typeddict_to_schema(Datum, out_dir=schema_out_dir)
    parse_typeddict_to_schema(EventDescriptor, out_dir=schema_out_dir)
    parse_typeddict_to_schema(EventPage, out_dir=schema_out_dir)
    parse_typeddict_to_schema(Event, out_dir=schema_out_dir)
    parse_typeddict_to_schema(Resource, out_dir=schema_out_dir)
    parse_typeddict_to_schema(RunStart, out_dir=schema_out_dir)
    parse_typeddict_to_schema(RunStop, out_dir=schema_out_dir)
    parse_typeddict_to_schema(StreamDatum, out_dir=schema_out_dir)
    parse_typeddict_to_schema(StreamResource, out_dir=schema_out_dir)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--schema_out_directory", default=SCHEMA_OUT_DIR, nargs="?")
    args = parser.parse_args()
    generate_all_schema(schema_out_dir=args.schema_out_directory)
