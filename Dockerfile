# The Lambda container image: one function that serves every flow.
#
# Build the wheel first (python -m build --wheel), then:
#   docker buildx build --platform linux/amd64 --provenance=false -t pidge .
# --provenance=false keeps the push a single-platform image manifest, which
# is what Lambda accepts. Then smoke-test it:
#   infra/scripts/smoke_image.sh pidge
#
# BASE_IMAGE can point at AWS's Docker Hub copy of the same image
# (amazon/aws-lambda-python:3.14-x86_64) where public.ecr.aws is unreachable.
ARG BASE_IMAGE=public.ecr.aws/lambda/python:3.14
FROM ${BASE_IMAGE}

COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt \
        --target ${LAMBDA_TASK_ROOT}

COPY dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir --no-deps --target ${LAMBDA_TASK_ROOT} /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

CMD ["pidge.lambda_main.lambda_handler"]
