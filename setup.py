import setuptools

setuptools.setup(
    name='pycvsps',
    version='1.0',
    author='Emanuele Giaquinta',
    author_email='emanuele.giaquinta@gmail.com',
    description='cvsps python port',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/exg/pycvsps',
    packages=setuptools.find_packages(),
    python_requires='>=3',
    entry_points={
        'console_scripts': [
            'cvsps = pycvsps.cvsps:main',
        ],
    },
    classifiers=[
        'LICENSE :: OSI APPROVED :: GNU GENERAL PUBLIC LICENSE V2 OR LATER (GPLV2+)',
    ],
)
