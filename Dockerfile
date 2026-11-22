# The Lambda container image: one function that serves every flow.
#
# Build the wheel first (python -m build --wheel), then:
#   docker buildx build --platform linux/amd64 --provenance=false -t pidge .
# --provenance=false keeps the push a single-platform image manifest, which
# is what Lambda accepts.
FROM public.ecr.aws/lambda/python:3.14

COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt \
        --target ${LAMBDA_TASK_ROOT}

COPY dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir --no-deps --target ${LAMBDA_TASK_ROOT} /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

CMD ["pidge.lambda_main.lambda_handler"]
